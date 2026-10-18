"""`plate` Wave 9 (router plan T103): run --packets, cards, estimate, export --runs / --accepted,
review --all-exportable, spend --by / --all-projects, doctor, recipes, and the Magnific smoke case.

One module-scoped world: the committed Blender packets `fixtures/packets/room_a` and `room_a_v2` run together by
`plate run --packets` on the daemon's own `FakeEngine(echo=True)` (`PLATE_FAKE_ENGINE=1`) under a throw-away
PLATE_HOME, so two DONE runs exist for the read-only commands. Tests that change a project (an accepted take, a
GATE_FAIL, a second run) make their own under the per-test PLATE_HOME. Every call is `cli.main(argv, env=...)`
in process with an explicit environment. No client data is read and no socket is opened.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from plate_daemon import cli
from plate_daemon.index import Index
from plate_daemon.machine import append_note, get_run
from plate_daemon.projects import Project

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
ROOM_V2 = ROOT / "fixtures" / "packets" / "room_a_v2"
STARTUP = ROOT / "plugins" / "max" / "startup" / "plate_startup.py"
NIGHT = "night_relight_changelist"
PROJECT_ID = "plate-fixture"  # the manifests' project_id
NOISE = json.dumps({"echo": False, "size": [640, 360]})
needs_rooms = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_V2)),
    reason="fixtures/packets/room_a and room_a_v2 not built yet (T2)",
)

# Switches that would change what a test means if a developer's shell had them exported.
SCRUBBED = (
    "PLATE_LIVE",
    "PLATE_FAKE_ENGINE",
    "PLATE_ROUTER_MODE",
    "PLATE_DEBUG",
    "PLATE_DAEMON_URL",
    "PLATE_PORT",
    "PLATE_MAX_STARTUP_DIR",
    "PLATE_PIXELS_INPROC",
    "PLATE_PIXELS_PORT",
)


def env_with(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if k not in SCRUBBED}, **extra}


def run_cli(env: dict[str, str], *argv: str, **kw) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), env=env, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


def packet_runs(out: str, state: str = "DONE") -> dict[str, str]:
    """{packet id: run id} of the `packet X: run Y STATE` lines `plate run --packets` prints."""
    return {m[1]: m[2] for m in re.finditer(rf"^packet (pkt_\w+): run (run_\w+) {state}\b", out, re.M)}


@dataclass
class World:
    env: dict[str, str]
    home: Path
    project: Path
    packets: dict[str, str]  # packet id -> run id, both DONE

    @property
    def run_ids(self) -> list[str]:
        return list(self.packets.values())

    def cli(self, *argv: str, **kw) -> tuple[int, str, str]:
        return run_cli(self.env, *argv, **kw)


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> World:
    if not all((p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_V2)):
        pytest.skip("fixtures/packets/room_a and room_a_v2 not built yet (T2)")
    base = tmp_path_factory.mktemp("cli_wave9")
    home = base / "home"
    mp = pytest.MonkeyPatch()
    for name in SCRUBBED:
        mp.delenv(name, raising=False)
    mp.setenv("PLATE_HOME", str(home))
    try:
        env = env_with(PLATE_HOME=str(home), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
        code, out, err = run_cli(
            env, "run", NIGHT, "--project", str(base / "proj"), "--packets", f"{ROOM_A},{ROOM_V2}",
            "--approve", "auto",
        )  # fmt: skip
        assert code == cli.EXIT_OK, (out, err)
        packets = packet_runs(out)
        assert len(packets) == 2, out
    finally:
        mp.undo()
    return World(env, home, base / "proj", packets)


def project_runs(home: Path, project_id: str = PROJECT_ID) -> dict[str, dict]:
    with Index(home).project(project_id) as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_utc, id")}


def accept(env: dict[str, str], run_id: str) -> None:
    """Mark a DONE run accepted the way `POST /runs/{id}/accept` does once the scrub is logged: the index note
    that `export --accepted` reads and the `done` ledger record that `spend` counts."""
    session = cli.Session(env=env, out=io.StringIO(), err=io.StringIO(), ask=lambda _p: "")
    pipeline = session.pipeline()
    pid = session.index.locate_run(run_id)
    note = {"scrub_log_id": "scrub_test", "decision": "accepted"}
    with session.index.project(pid) as conn:
        append_note(conn, run_id, kind="accept", actor="human", reason=json.dumps(note, sort_keys=True))
        pipeline.ledger(pid, "done", get_run(conn, run_id), by="human", data=note)


@pytest.fixture
def one_run(tmp_path):
    """A project of its own under the per-test PLATE_HOME with one DONE run of room_a."""
    if not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    code, out, err = run_cli(
        env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packet", str(ROOM_A), "--approve", "auto"
    )
    assert code == cli.EXIT_OK, (out, err)
    return env, tmp_path / "proj", re.search(r"^run (run_\w+) DONE", out, re.M).group(1)


# ---------------------------------------------------------------------------- plate run --packets


@needs_rooms
def test_run_packets_runs_each_packet_in_its_own_column_and_prints_a_line_per_packet(world):
    assert set(world.packets) == {
        json.loads((p / "manifest.json").read_text(encoding="utf-8"))["packet_id"] for p in (ROOM_A, ROOM_V2)
    }
    graph = json.loads((world.project / "project.json").read_text(encoding="utf-8"))["graph"]
    by_kind = {k: [n for n in graph["nodes"] if n["kind"] == k] for k in ("Plate", "Mask", "Edit")}
    assert {k: len(v) for k, v in by_kind.items()} == {"Plate": 2, "Mask": 2, "Edit": 2}
    edits = sorted(by_kind["Edit"], key=lambda n: n["position"]["x"])
    assert [n["position"]["x"] for n in edits] == [0, graph_build_column_width()]
    assert all(
        n["id"].startswith("edit_cam_a_") for n in edits
    )  # graph_build's ids, not the old edit-<recipe>
    # the Edit takes the plate as its image and the Mask as its mask, by named handles
    for e in edits:
        wired = {(x["targetHandle"], x["sourceHandle"]) for x in graph["edges"] if x["target"] == e["id"]}
        assert wired == {("image", "image"), ("mask", "mask")}
    runs = project_runs(world.home)
    assert sorted(r["state"] for r in runs.values()) == ["DONE", "DONE"]
    assert {r["packet_id"] for r in runs.values()} == set(world.packets)


def graph_build_column_width() -> int:
    from plate_daemon import graph_build

    return graph_build.COLUMN_W


@needs_rooms
def test_a_second_run_of_the_same_packets_is_another_attempt_of_the_same_edits(world, tmp_path):
    # its own project and home: the shared world stays as it was
    env = env_with(PLATE_HOME=str(tmp_path / "home"), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    for _ in range(2):
        code, out, err = run_cli(
            env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packets", f"{ROOM_A},{ROOM_V2}",
            "--approve", "auto",
        )  # fmt: skip
        assert code == cli.EXIT_OK, (out, err)
    graph = json.loads((tmp_path / "proj" / "project.json").read_text(encoding="utf-8"))["graph"]
    assert len(graph["nodes"]) == 6  # the chains were reused, not laid again
    runs = project_runs(tmp_path / "home").values()
    assert sorted(r["attempt"] for r in runs) == [1, 1, 2, 2]


def test_run_needs_exactly_one_of_packet_and_packets(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path))
    base = ("run", NIGHT, "--project", str(tmp_path / "proj"))
    code, out, err = run_cli(env, *base)
    assert code == cli.EXIT_USAGE and "--packet" in err and "--packets" in err
    code, out, err = run_cli(env, *base, "--packet", str(ROOM_A), "--packets", str(ROOM_V2))
    assert code == cli.EXIT_USAGE and "not both" in err
    code, out, err = run_cli(env, *base, "--packets", " , ")
    assert code == cli.EXIT_USAGE
    assert not (tmp_path / "proj").exists()  # refused before anything was made


@needs_rooms
def test_run_packets_exits_2_when_a_packet_ends_gate_fail(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE=NOISE, PYTHONUTF8="1")
    code, out, err = run_cli(
        env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packets", f"{ROOM_A},{ROOM_V2}",
        "--approve", "auto",
    )  # fmt: skip
    assert code == cli.EXIT_GATE_FAIL, (out, err)
    failed = packet_runs(out, "GATE_FAIL")
    assert len(failed) == 2 and "the gates failed on 2 run(s)" in out
    # nothing that failed its gates leaves in a batch without an override recorded first
    code, out, err = run_cli(
        env, "export", "--runs", ",".join(failed.values()), "--to", str(tmp_path / "out")
    )
    assert code == cli.EXIT_GATE_FAIL and err.count("GATE_FAIL") >= 2, (out, err)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    code, out, err = run_cli(env, "review", str(tmp_path / "proj"), "--all-exportable")
    assert code == cli.EXIT_USAGE and "no exportable run" in err


@needs_rooms
def test_a_refused_packet_is_a_line_of_its_own_and_the_next_packet_still_runs(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    answers = iter(["n", "y"])
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    code, out, err = run_cli(
        env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packets", f"{ROOM_A},{ROOM_V2}", ask=ask
    )
    assert len(asked) == 2 and all("0.0400 USD" in p for p in asked)  # asked packet by packet
    assert code == cli.EXIT_REFUSED, (out, err)
    assert "not run (not approved: nothing was sent)" in out
    assert len(packet_runs(out)) == 1
    states = sorted(r["state"] for r in project_runs(tmp_path).values())
    assert states == ["CANCELLED", "DONE"]


@needs_rooms
def test_one_envelope_is_the_ceiling_for_the_whole_command(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    # two packets at 0.04 USD each: 0.05 covers the first and not the second
    code, out, err = run_cli(
        env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packets", f"{ROOM_A},{ROOM_V2}",
        "--approve", "auto", "--envelope", "0.05",
    )  # fmt: skip
    assert code == cli.EXIT_REFUSED, (out, err)
    assert out.count("at most 0.0500 USD for the whole command") == 1  # one envelope, made once
    assert len(packet_runs(out)) == 1 and "exceeds envelope" in err
    assert sorted(r["state"] for r in project_runs(tmp_path).values()) == ["CANCELLED", "DONE"]


@needs_rooms
def test_an_unknown_packet_ends_only_its_own_line(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    code, out, err = run_cli(
        env, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packets", f"{ROOM_A},pkt_UNKNOWN",
        "--approve", "auto",
    )  # fmt: skip
    assert code == cli.EXIT_USAGE, (out, err)
    assert len(packet_runs(out)) == 1
    assert "packet pkt_UNKNOWN: not run (packet 'pkt_UNKNOWN' is neither a packet folder" in out


# ---------------------------------------------------------------------------- plate cards / estimate


@needs_rooms
def test_cards_print_a_header_and_a_row_after_a_scored_run(world):
    code, out, err = world.cli("cards", "--project", str(world.project))
    assert code == cli.EXIT_OK, (out, err)
    lines = out.strip().splitlines()
    assert lines[0] == f"engine cards for {PROJECT_ID}"
    assert lines[1].split() == [
        "engine",
        "task",
        "shot_class",
        "n",
        "pass",
        "lower95",
        "mean_usd",
        "steering",
    ]
    from plate_engines.cards import wilson_lower

    assert lines[2].split() == [
        "fake",
        "relight",
        "wide",
        "2",
        "2",
        f"{wilson_lower(2, 2):.4f}",
        "0.0400",
        "no",
    ]
    # by id, and with no project at all (every project this machine knows)
    code, out2, _ = world.cli("cards", "--project", PROJECT_ID)
    assert code == cli.EXIT_OK and out2.splitlines()[1:] == lines[1:]
    code, out3, _ = world.cli("cards")
    assert code == cli.EXIT_OK and out3.splitlines()[0] == "engine cards for all projects"
    assert out3.splitlines()[2].split()[:3] == ["fake", "relight", "wide"]
    # the filters
    code, out4, _ = world.cli("cards", "--project", PROJECT_ID, "--shot-class", "close")
    assert code == cli.EXIT_OK and "no cards yet" in out4 and "fake" not in out4
    code, out5, _ = world.cli("cards", "--project", PROJECT_ID, "--task", "relight", "--shot-class", "wide")
    assert out5.splitlines()[2:] == lines[2:]


def test_cards_on_a_machine_with_no_projects_is_a_header_and_a_hint(tmp_path):
    code, out, err = run_cli(env_with(PLATE_HOME=str(tmp_path)), "cards")
    assert code == cli.EXIT_OK, err
    assert "engine  task  shot_class  n  pass  lower95  mean_usd  steering" in out and "no cards yet" in out


def test_estimate_on_an_empty_cell_divides_by_one(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path))
    code, out, err = run_cli(
        env,
        "estimate",
        "--engine",
        "nano-banana-pro",
        "--task",
        "relight",
        "--shot-class",
        "wide",
        "--count",
        "3",
    )
    assert code == cli.EXIT_OK, err
    assert "no card for this cell: dividing by 1" in out
    assert "quote 0.2400 USD each; 3 x = 0.7200 USD" in out
    assert "expected USD per accepted output: 0.2400" in out
    assert "expected USD for 3 accepted outputs: 0.7200" in out


@needs_rooms
def test_estimate_divides_by_the_cards_lower_bound(world):
    from plate_engines.cards import wilson_lower

    code, out, err = world.cli(
        "estimate", "--engine", "fake", "--task", "relight", "--shot-class", "wide", "--count", "1",
        "--project", str(world.project),
    )  # fmt: skip
    assert code == cli.EXIT_OK, err
    lower = wilson_lower(2, 2)
    assert f"card: n 2, 2 passed, lower95 {lower:.4f} (not steering yet" in out
    assert f"expected USD per accepted output: {0.04 / lower:.4f}" in out
    assert "dividing by 1" not in out
    # a different cell of the same engine has no card
    code, out, err = world.cli(
        "estimate", "--engine", "fake", "--task", "relight", "--shot-class", "close", "--count", "1",
        "--project", PROJECT_ID,
    )  # fmt: skip
    assert "no card for this cell: dividing by 1" in out and "output: 0.0400" in out


def test_estimate_seconds_is_for_per_second_engines_and_count_for_the_rest(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path))
    base = ("estimate", "--task", "relight", "--shot-class", "wide")
    code, out, err = run_cli(env, *base, "--engine", "kling-o3-4k", "--seconds", "5")
    assert code == cli.EXIT_OK, err
    assert "quote 1.7850 USD for 5 s" in out and "expected USD per accepted output: 1.7850" in out
    code, out, err = run_cli(env, *base, "--engine", "nano-banana-pro", "--seconds", "5")
    assert code == cli.EXIT_USAGE and "priced per image, not per second" in err
    code, out, err = run_cli(env, *base, "--engine", "kling-o3-4k", "--count", "2")
    assert code == cli.EXIT_USAGE and "use --seconds" in err
    code, out, err = run_cli(env, *base, "--engine", "no-such-engine", "--count", "1")
    assert code == cli.EXIT_USAGE and "unknown engine" in err
    code, out, err = run_cli(env, *base, "--engine", "fake", "--seconds", "5")
    assert code == cli.EXIT_USAGE
    for bad in (("--count", "0"), ("--seconds", "0"), ("--seconds", "-3")):
        code, out, err = run_cli(env, *base, "--engine", "kling-o3-4k", *bad)
        assert code == cli.EXIT_USAGE, bad
    # exactly one of --count and --seconds
    for extra in ((), ("--count", "1", "--seconds", "5")):
        code, out, err = run_cli(env, *base, "--engine", "fake", *extra)
        assert code == cli.EXIT_USAGE


def test_estimate_with_a_zero_lower_bound_is_unbounded(tmp_path, monkeypatch):
    from plate_engines.cards import Card

    monkeypatch.setattr(cli, "_cards", lambda _s, _p: [Card.make("fake", "relight", "wide", 0, 4)])
    code, out, err = run_cli(
        env_with(PLATE_HOME=str(tmp_path)),
        "estimate", "--engine", "fake", "--task", "relight", "--shot-class", "wide", "--count", "1",
    )  # fmt: skip
    assert code == cli.EXIT_OK, err
    assert "n 4, 0 passed, lower95 0.0000" in out and "unbounded" in out


# ---------------------------------------------------------------------------- plate export --runs / --accepted


@needs_rooms
def test_export_runs_writes_each_take_with_its_sidecar_and_never_overwrites(world, tmp_path):
    out_dir = tmp_path / "out"
    code, out, err = world.cli("export", "--runs", ",".join(world.run_ids), "--to", str(out_dir))
    assert code == cli.EXIT_OK, (out, err)
    assert len(list(out_dir.glob("*.tif"))) == 2 and len(list(out_dir.glob("*.gate.json"))) == 2
    assert "exported 2 run(s)" in out and "0 refused" in out
    # the default name is {node}_{run8}
    assert all(re.fullmatch(r"edit_cam_a_[12]_\w{8}\.tif", p.name) for p in out_dir.glob("*.tif"))
    code, out, err = world.cli("export", "--runs", ",".join(world.run_ids), "--to", str(out_dir))
    assert code == cli.EXIT_ERROR and err.count("already exists") == 2, (out, err)
    # one name pattern, one format
    png_dir = tmp_path / "png"
    code, out, err = world.cli(
        "export", "--runs", world.run_ids[0], "--to", str(png_dir), "--format", "png16",
        "--name-pattern", "take_{n}_{engine}",
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    assert [p.name for p in png_dir.glob("*.png")] == ["take_001_fake.png"]


@needs_rooms
def test_export_runs_by_folder_when_the_index_does_not_know_them(world, tmp_path):
    other = {**world.env, "PLATE_HOME": str(tmp_path / "other-home")}
    code, out, err = run_cli(other, "export", "--runs", world.run_ids[0], "--to", str(tmp_path / "o"))
    assert code == cli.EXIT_USAGE and "name its folder with --project" in err
    code, out, err = run_cli(
        other,
        "export",
        "--runs",
        world.run_ids[0],
        "--to",
        str(tmp_path / "o"),
        "--project",
        str(world.project),
    )
    assert code == cli.EXIT_OK, (out, err)  # the index was rebuilt from the folder
    code, out, err = run_cli(
        other, "export", "--runs", "run_NOPE", "--to", str(tmp_path / "o2"), "--project", str(world.project)
    )
    assert code == cli.EXIT_USAGE and "run_NOPE" in err


@needs_rooms
def test_export_accepted_writes_only_the_accepted_take(one_run, tmp_path):
    env, proj, rid = one_run
    out_dir = tmp_path / "accepted"
    code, out, err = run_cli(env, "export", "--project", str(proj), "--accepted", "--to", str(out_dir))
    assert code == cli.EXIT_OK and "no accepted runs yet" in out and not out_dir.exists()
    accept(env, rid)
    code, out, err = run_cli(env, "export", "--project", str(proj), "--accepted", "--to", str(out_dir))
    assert code == cli.EXIT_OK, (out, err)
    assert [p.suffix for p in sorted(out_dir.iterdir())] == [".json", ".tif"]
    assert "exported 1 run(s)" in out
    # --runs with --accepted also wants the accept note
    other = tmp_path / "other"
    code, out, err = run_cli(
        env, "export", "--project", str(proj), "--runs", rid, "--accepted", "--to", str(other)
    )
    assert code == cli.EXIT_USAGE  # exactly one of the three forms


def test_export_forms_are_exclusive_and_a_batch_has_no_typed_override(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path))
    to = str(tmp_path / "o")
    for argv in (
        ["export", "--to", to],
        ["export", "run_1", "--runs", "run_2", "--to", to],
        ["export", "--accepted", "--to", to],  # --accepted needs --project
        ["export", "--runs", "run_1", "--override-reason", "client signed off the drift", "--to", to],
        ["export", "--runs", "run_1", "--name", "x", "--to", to],
        ["export", "run_1", "--name-pattern", "{n}", "--to", to],
        ["export", "--runs", " , ", "--to", to],
    ):
        code, out, err = run_cli(env, *argv)
        assert code == cli.EXIT_USAGE, (argv, out, err)


# ---------------------------------------------------------------------------- plate review --all-exportable


@needs_rooms
def test_review_all_exportable_bundles_every_take_the_guard_allows(world, tmp_path):
    out_dir = tmp_path / "bundle"
    code, out, err = world.cli("review", str(world.project), "--all-exportable", "--out", str(out_dir))
    assert code == cli.EXIT_OK, (out, err)
    (zip_path,) = out_dir.glob("*.zip")
    assert "2 take(s)" in out and str(zip_path) in out
    with zipfile.ZipFile(zip_path) as zf:
        assert any(n.endswith(".html") for n in zf.namelist())
    code, out, err = world.cli("review", str(world.project), "--out", str(out_dir))
    assert code == cli.EXIT_USAGE and "--all-exportable" in err
    code, out, err = world.cli("review", str(world.project), "--runs", world.run_ids[0], "--all-exportable")
    assert code == cli.EXIT_USAGE


@needs_rooms
def test_review_all_exportable_skips_a_previs_take_and_says_why(one_run, tmp_path):
    env, proj, rid = one_run
    home = Path(env["PLATE_HOME"])
    with Index(home).project(PROJECT_ID) as conn:
        row = conn.execute("SELECT params FROM runs WHERE id = ?", (rid,)).fetchone()
        params = json.loads(row[0] or "{}")
        params["previs"] = True
        conn.execute("UPDATE runs SET params = ? WHERE id = ?", (json.dumps(params), rid))
        conn.commit()
    code, out, err = run_cli(env, "review", str(proj), "--all-exportable", "--out", str(tmp_path / "b"))
    assert code == cli.EXIT_USAGE and f"skipped {rid}: not reviewable (PREVIS_NOT_DELIVERABLE)" in out, (
        out,
        err,
    )


# ---------------------------------------------------------------------------- plate spend --by / --all-projects


def this_week() -> str:
    iso = datetime.now(UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@needs_rooms
def test_spend_by_week_prints_the_runs_bucket(world):
    code, out, err = world.cli("spend", str(world.project), "--by", "week")
    assert code == cli.EXIT_OK, (out, err)
    lines = out.strip().splitlines()
    assert lines[0] == f"spend for {PROJECT_ID} by week"
    assert lines[1].split() == ["week", "engine", "runs", "quoted", "USD", "billed", "USD", "accepted"]
    assert lines[2].split() == [this_week(), "fake", "2", "0.0800", "0.0000", "0"]
    assert lines[3].split() == ["total", "2", "0.0800", "0.0000", "0"]
    code, out, err = world.cli("spend", PROJECT_ID, "--by", "day")
    assert code == cli.EXIT_OK and datetime.now(UTC).date().isoformat() in out.splitlines()[2]
    code, out, err = world.cli("spend", PROJECT_ID, "--by", "engine")
    assert out.splitlines()[0] == f"spend for {PROJECT_ID}"  # the default table, unchanged
    assert out.splitlines()[2].split() == ["fake", "2", "0.0800", "0.0000", "0"]
    code, out, err = world.cli("spend", PROJECT_ID, "--by", "month")
    assert code == cli.EXIT_USAGE


@needs_rooms
def test_spend_all_projects_by_week_reads_every_project(world):
    code, out, err = world.cli("spend", "--all-projects", "--by", "week")
    assert code == cli.EXIT_OK, (out, err)
    lines = out.strip().splitlines()
    assert lines[0] == "spend for all projects (1) by week"
    assert lines[2].split() == [this_week(), "fake", "2", "0.0800", "0.0000", "0"]
    for argv in (["spend"], ["spend", PROJECT_ID, "--all-projects"]):
        code, out, err = world.cli(*argv)
        assert code == cli.EXIT_USAGE and "--all-projects" in err
    code, out, err = world.cli("spend", "--all-projects", "--since", "2099-01-01")
    assert code == cli.EXIT_OK and out.strip().splitlines()[-1].split()[:2] == ["total", "0"]


@needs_rooms
def test_spend_shows_cost_per_accepted_once_something_is_accepted(one_run):
    env, proj, rid = one_run
    code, out, err = run_cli(env, "spend", str(proj))
    assert code == cli.EXIT_OK and "per accepted" not in out  # nothing accepted: the T78 table
    accept(env, rid)
    code, out, err = run_cli(env, "spend", str(proj))
    lines = out.strip().splitlines()
    assert lines[1].split() == [
        "engine",
        "runs",
        "quoted",
        "USD",
        "billed",
        "USD",
        "accepted",
        "per",
        "accepted",
        "USD",
    ]
    assert lines[2].split() == ["fake", "1", "0.0400", "0.0000", "1", "0.0400"]
    assert lines[3].split() == ["total", "1", "0.0400", "0.0000", "1", "0.0400"]
    code, out, err = run_cli(env, "spend", str(proj), "--by", "week")
    assert out.strip().splitlines()[2].split() == [
        this_week(),
        "fake",
        "1",
        "0.0400",
        "0.0000",
        "1",
        "0.0400",
    ]
    code, out, err = run_cli(env, "spend", "--all-projects", "--by", "week")
    assert out.strip().splitlines()[2].split()[-1] == "0.0400"


# ---------------------------------------------------------------------------- plate doctor


def doctor(tmp_path, **extra: str) -> tuple[int, str, dict[str, str]]:
    code, out, err = run_cli(env_with(PLATE_HOME=str(tmp_path), **extra), "doctor")
    rows = {}
    for line in out.splitlines()[1:]:
        name, _, rest = line.partition("  ")
        rows[name.strip()] = line
    assert err == ""
    return code, out, rows


def test_doctor_exits_0_mentions_ffmpeg_and_changes_nothing(tmp_path):
    home = tmp_path / "home"  # does not exist: a check must not make it
    code, out, err = run_cli(env_with(PLATE_HOME=str(home)), "doctor")
    assert code == cli.EXIT_OK and err == ""
    lines = out.splitlines()
    assert lines[0].split() == ["check", "result"]
    for name in ("git", "max startup script", "ui build", "ffmpeg", "ffprobe", "pixels worker", "token"):
        assert any(ln.startswith(name) for ln in lines[1:]), name
    assert "ffmpeg" in out
    assert not home.exists()
    assert any("no token to ask with" in ln or "not checked" in ln for ln in lines if ln.startswith("pixels"))
    assert any("no " in ln and "config.json yet" in ln for ln in lines if ln.startswith("token"))


def test_doctor_compares_the_startup_script_with_the_installed_copy(tmp_path):
    startup = tmp_path / "startup"
    startup.mkdir()
    code, out, rows = doctor(tmp_path, PLATE_MAX_STARTUP_DIR=str(startup))
    assert code == 0 and "not installed" in rows["max startup script"]  # tolerated
    shutil.copyfile(STARTUP, startup / "plate_startup.py")
    code, out, rows = doctor(tmp_path, PLATE_MAX_STARTUP_DIR=str(startup))
    assert code == 0 and "same as the installed copy" in rows["max startup script"]
    (startup / "plate_startup.py").write_bytes(STARTUP.read_bytes() + b"\n# edited in Max\n")
    code, out, rows = doctor(tmp_path, PLATE_MAX_STARTUP_DIR=str(startup))
    assert code == 0 and "differs from the installed copy" in rows["max startup script"]
    # config.json can name the folder instead
    (tmp_path / "config.json").write_text(
        json.dumps({"token": "t" * 24, "startup_dir": str(startup)}), encoding="utf-8", newline="\n"
    )
    code, out, rows = doctor(tmp_path)
    assert "differs from the installed copy" in rows["max startup script"]
    assert "config.json" in rows["token"] and "t" * 24 not in out  # the token is never printed


def test_doctor_reports_the_ui_build_and_missing_tools_without_failing(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    code, out, rows = doctor(tmp_path, PLATE_UI_DIST=str(dist))
    assert "is missing" in rows["ui build"]
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    code, out, rows = doctor(tmp_path, PLATE_UI_DIST=str(dist))
    assert "is there" in rows["ui build"]
    monkeypatch.setenv("PLATE_FFMPEG", str(tmp_path / "no-ffmpeg"))
    monkeypatch.setenv("PLATE_FFPROBE", str(tmp_path / "no-ffprobe"))
    code, out, rows = doctor(tmp_path)
    assert code == 0 and "ffmpeg not found" in rows["ffmpeg"] and "ffprobe not found" in rows["ffprobe"]


def test_doctor_tolerates_no_git_and_no_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_git_out", lambda *a: None)
    code, out, rows = doctor(tmp_path)
    assert code == 0 and "git was not found" in rows["git"]
    answers = {
        "rev-parse HEAD": ("0123456789abcdef" * 2 + "01234567\n", 0),
        "rev-parse --verify --quiet origin/main": ("", 1),
    }

    def fake_git(*argv):
        text, code_ = answers[" ".join(argv)]
        return type("R", (), {"returncode": code_, "stdout": text})()

    monkeypatch.setattr(cli, "_git_out", fake_git)
    code, out, rows = doctor(tmp_path)
    assert code == 0 and "no remote" in rows["git"] and "01234567" in rows["git"]
    answers["rev-parse --verify --quiet origin/main"] = ("f" * 40 + "\n", 0)
    answers["rev-list --left-right --count HEAD...origin/main"] = ("2\t5\n", 0)
    code, out, rows = doctor(tmp_path)
    assert "is not origin/main" in rows["git"] and "ahead 2, behind 5" in rows["git"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_doctor_asks_the_pixels_worker_in_the_clients_own_modes(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"token": "k" * 24}), encoding="utf-8", newline="\n")
    code, out, rows = doctor(tmp_path, PLATE_PIXELS_INPROC="1")
    assert code == 0 and "inproc" in rows["pixels worker"]
    code, out, rows = doctor(tmp_path, PLATE_PIXELS_PORT=str(free_port()))
    assert code == 0 and "unavailable" in rows["pixels worker"] and "plate_pixels" in rows["pixels worker"]


def test_doctor_reads_a_token_file_without_printing_it(tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("s" * 40, encoding="utf-8")
    code, out, rows = doctor(tmp_path, PLATE_TOKEN_FILE=str(token_file), PLATE_PIXELS_INPROC="1")
    assert "PLATE_TOKEN_FILE" in rows["token"] and "s" * 40 not in out and "inproc" in rows["pixels worker"]
    code, out, rows = doctor(tmp_path, PLATE_TOKEN_FILE=str(tmp_path / "gone.txt"))
    assert code == 0 and "cannot be read" in rows["token"]


# ---------------------------------------------------------------------------- plate recipes


def night_recipe_as(tmp_path: Path, recipe_id: str, **changes) -> Path:
    import yaml

    from plate_daemon import recipes

    doc = yaml.safe_load((recipes.RECIPES_DIR / f"{NIGHT}.yaml").read_text(encoding="utf-8"))
    doc.update(id=recipe_id, title=f"My {recipe_id}", **changes)
    path = tmp_path / f"{recipe_id}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def test_recipes_list_shows_the_shipped_recipes(tmp_path):
    code, out, err = run_cli(env_with(PLATE_HOME=str(tmp_path)), "recipes", "list")
    assert code == cli.EXIT_OK, err
    lines = out.splitlines()
    assert lines[0].split() == ["id", "kind", "engine", "shot_class", "origin", "title"]
    row = next(ln for ln in lines if ln.startswith(NIGHT))
    assert "still_edit" in row and "shipped" in row


def test_recipes_save_writes_to_the_studio_folder_of_the_home_and_refuses_a_second_save(tmp_path):
    home = tmp_path / "home"
    env = env_with(PLATE_HOME=str(home))
    src = night_recipe_as(tmp_path, "my_night")
    code, out, err = run_cli(env, "recipes", "save", str(src), "--scope", "studio")
    assert code == cli.EXIT_OK, (out, err)
    saved = home / "recipes" / "my_night.yaml"
    assert saved.is_file() and f"saved recipe my_night (studio) at {saved}" in out
    code, out, err = run_cli(env, "recipes", "list")
    assert any(ln.startswith("my_night") and "studio" in ln for ln in out.splitlines())
    code, out, err = run_cli(env, "recipes", "save", str(src), "--scope", "studio")
    assert code == cli.EXIT_USAGE and "already exists" in err
    code, out, err = run_cli(env, "recipes", "save", str(src), "--scope", "studio", "--overwrite")
    assert code == cli.EXIT_OK, (out, err)


def test_recipes_save_to_a_project_and_the_refusals(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path / "home"))
    proj = tmp_path / "proj"
    Project.create(proj, "demo-proj", {"nda": False, "allowed_grades": ["C0", "C1", "C2"]})
    src = night_recipe_as(tmp_path, "proj_night")
    code, out, err = run_cli(env, "recipes", "save", str(src), "--scope", "project")
    assert code == cli.EXIT_USAGE and "--project" in err
    code, out, err = run_cli(env, "recipes", "save", str(src), "--scope", "project", "--project", str(proj))
    assert code == cli.EXIT_OK, (out, err)
    assert (proj / "recipes" / "proj_night.yaml").is_file()
    code, out, err = run_cli(env, "recipes", "list", "--project", str(proj))
    assert any(ln.startswith("proj_night") and "project" in ln for ln in out.splitlines())
    code, out, err = run_cli(env, "recipes", "list")
    assert not any(
        ln.startswith("proj_night") for ln in out.splitlines()
    )  # a project's own, not the studio's
    bad_engine = night_recipe_as(tmp_path, "bad_engine", engine="no-such-engine")
    code, out, err = run_cli(env, "recipes", "save", str(bad_engine), "--scope", "studio")
    assert code == cli.EXIT_USAGE and "not an engine manifest" in err
    junk = tmp_path / "junk.yaml"
    junk.write_text("kind: [unclosed", encoding="utf-8")
    code, out, err = run_cli(env, "recipes", "save", str(junk), "--scope", "studio")
    assert code == cli.EXIT_USAGE
    code, out, err = run_cli(env, "recipes", "save", str(tmp_path / "missing.yaml"), "--scope", "studio")
    assert code == cli.EXIT_USAGE and "cannot read" in err
    code, out, err = run_cli(env, "recipes")
    assert code == cli.EXIT_USAGE


# ---------------------------------------------------------------------------- plate smoke: the Magnific case

MAGNIFIC = ("magnific-precision-v2", "magnific")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the smoke opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_the_magnific_case_is_live_only_and_sends_a_tiny_png():
    case = cli.SMOKE_CASE_BY_ROUTE[MAGNIFIC]
    assert case.live_only and case.image_px and case.image_px <= 256
    assert case.keys == (("MAGNIFIC_API_KEY", "FREEPIK_API_KEY"),)
    png = cli.smoke_png(case.image_px)
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.shape[:2] == (case.image_px, case.image_px)
    assert len(png) < len(cli.smoke_png())  # smaller than the fixture room


def test_the_fixture_smoke_skips_magnific_with_its_reason_and_keeps_the_count(tmp_path, no_network):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1")
    code, out, err = run_cli(env, "smoke")
    assert code == cli.EXIT_OK, (out, err)
    row = next(ln for ln in out.splitlines() if ln.startswith("magnific-precision-v2 via magnific"))
    assert row.startswith("magnific-precision-v2 via magnific: skipped (live only: ")
    m = re.fullmatch(r"fixture smoke: (\d+) adapters ok", out.strip().splitlines()[-1])
    assert m and int(m.group(1)) >= 11, out  # the count before the case was added


def live_env(**extra: str) -> dict[str, str]:
    return env_with(PLATE_LIVE="1", MAGNIFIC_API_KEY="k", **extra)


def test_a_live_smoke_with_a_recorded_transport_never_sends_the_magnific_case(tmp_path, no_network):
    built: list[cli.ReplayTransport] = []

    def factory(case, manifest, route, live):
        built.append(cli.ReplayTransport(case.contract, case.prefer))
        return built[-1]

    code, out, err = run_cli(
        live_env(PLATE_HOME=str(tmp_path)),
        "smoke", "--live", "--budget-usd", "5", "--engines", "magnific-precision-v2",
        transport_factory=factory,
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    assert "magnific-precision-v2 via magnific: skipped (live only: " in out
    assert built and all(t.served == [] for t in built)
    assert out.count("magnific-precision-v2 via magnific") == 1  # the row is printed once


def test_a_live_magnific_smoke_builds_a_tiny_png_and_never_guesses_the_model_id(
    tmp_path, monkeypatch, no_network
):
    sizes: list[int | None] = []
    real = cli.smoke_png
    monkeypatch.setattr(cli, "smoke_png", lambda square_px=None: sizes.append(square_px) or real(square_px))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    code, out, err = run_cli(
        live_env(PLATE_HOME=str(tmp_path)),
        "smoke", "--live", "--budget-usd", "5", "--engines", "magnific-precision-v2",
        transport_factory=lambda case, manifest, route, live: httpx.MockTransport(handler),
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    assert "magnific-precision-v2 via magnific: skipped (no vendor model id yet" in out
    assert 128 in sizes and seen == []  # the tiny PNG was made; nothing was sent
    # no key, no call
    code, out, err = run_cli(
        env_with(PLATE_HOME=str(tmp_path), PLATE_LIVE="1"),
        "smoke", "--live", "--budget-usd", "5", "--engines", "magnific-precision-v2",
        transport_factory=lambda case, manifest, route, live: httpx.MockTransport(handler),
    )  # fmt: skip
    assert "skipped (set MAGNIFIC_API_KEY/FREEPIK_API_KEY)" in out and seen == []
