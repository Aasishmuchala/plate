"""`plate` (router plan T34): run / gate / export / reindex / gc / pack / smoke, in process.

Every test calls `cli.main(argv, env=...)` against a throw-away PLATE_HOME and the committed Blender packet
`fixtures/packets/room_a`; the vendor is the daemon's in-process `FakeEngine(echo=True)` (`PLATE_FAKE_ENGINE=1`), which echoes
the plate egress prepared so the gates score real pixels (`{"echo": false, "size": [640, 360]}` is its noise,
which is how a GATE_FAIL run is made). The smoke runs on the recorded vendor fixtures with the socket layer
blocked; `smoke --live` is exercised with an injected transport and never reaches a network.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from plate_daemon import cli, lineage
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index
from plate_daemon.machine import run_events
from plate_daemon.projects import Project

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
NIGHT = "night_relight_changelist"
NOISE = json.dumps({"echo": False, "size": [640, 360]})
needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


# Switches that would change what a test means if a developer's shell had them exported.
SCRUBBED = ("PLATE_LIVE", "PLATE_FAKE_ENGINE", "PLATE_ROUTER_MODE", "PLATE_DEBUG")


def env_with(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if k not in SCRUBBED}, **extra}


@pytest.fixture
def fake_env() -> dict[str, str]:
    return env_with(PLATE_FAKE_ENGINE="1")


def plate(capsys, *argv: str, env: dict[str, str] | None = None, **kw) -> tuple[int, str, str]:
    code = cli.main(list(argv), env=env if env is not None else env_with(), **kw)
    out, err = capsys.readouterr()
    return code, out, err


def run_recipe(capsys, tmp_path: Path, env: dict[str, str], *extra: str, recipe: str = NIGHT, **kw):
    return plate(
        capsys,
        "run",
        recipe,
        "--project",
        str(tmp_path / "proj"),
        "--packet",
        str(ROOM_A),
        *extra,
        env=env,
        **kw,
    )


def project_runs(tmp_path: Path, project_id: str = "plate-fixture") -> dict[str, dict]:
    index = Index(Settings.load().home)
    with index.project(project_id) as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_utc, id")}


def run_id_in(out: str, state: str = "DONE") -> str:
    m = re.search(rf"^run (run_\w+) {state}\b", out, re.M)
    assert m, out
    return m.group(1)


# ---------------------------------------------------------------------------- the fake engine switch


def test_fake_engine_env_is_off_on_or_json():
    assert cli.fake_engine_from_env({}) is None
    assert cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": "0"}) is None
    on = cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": "1"})
    assert type(on) is FakeEngine and on.echo and on.shift_px == 0
    flagged = cli.fake_engine_from_env(
        {"PLATE_FAKE_ENGINE": '{"echo": false, "shift_px": 3, "size": [8, 8]}'}
    )
    assert not flagged.echo and flagged.shift_px == 3 and flagged.size == (8, 8)
    for bad in ("{", "[1]", '{"nope": 1}', '{"behaviour": "sulk"}'):
        with pytest.raises(cli.CliExit) as e:
            cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": bad})
        assert e.value.code == cli.EXIT_USAGE


def test_echo_engine_hands_back_the_png_it_was_sent():
    eng = FakeEngine(echo=True)
    import base64

    png = bytes(cv2.imencode(".png", np.full((4, 6, 3), 99, np.uint8))[1])
    job = eng.submit({"inputs": {"image": {"b64": base64.b64encode(png).decode()}}}, "k1")
    assert eng.fetch(f"fake://results/{job}") == png


# ---------------------------------------------------------------------------- plate run


@needs_room_a
def test_run_reaches_done_and_leaves_the_folder_the_daemon_reads(capsys, tmp_path, fake_env, settings, auth):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    assert code == cli.EXIT_OK, (out, err)
    assert "custody C0" in out and "price 0.0400 USD" in out  # the quote is printed before the run
    assert "mask: sky, vegetation, people ->" in out
    rid = run_id_in(out)
    proj = tmp_path / "proj"
    assert (proj / "project.json").is_file() and (proj / "packets.json").is_file()
    assert (proj / "gates" / f"{rid}.1.json").is_file()
    kinds = [
        json.loads(line)["kind"] for line in (proj / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert kinds[0] == "quoted" and kinds[-1] == "done" and "approved" in kinds and "submitted" in kinds
    graph = json.loads((proj / "project.json").read_text(encoding="utf-8"))["graph"]
    assert {n["kind"] for n in graph["nodes"]} == {"Plate", "Mask", "Edit"}
    # the same index serves the daemon: the run is there, DONE
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings), base_url="http://127.0.0.1:4610") as c:
        got = c.get(f"/api/v0/runs/{rid}", headers=auth)
        assert got.status_code == 200 and got.json()["state"] == "DONE"
    assert Index(settings.home).locate_run(rid) == "plate-fixture"


@needs_room_a
def test_a_second_run_is_a_new_attempt_not_a_duplicate(capsys, tmp_path, fake_env):
    for _ in range(2):
        code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
        assert code == cli.EXIT_OK, (out, err)
    runs = project_runs(tmp_path)
    assert sorted(r["attempt"] for r in runs.values()) == [1, 2]
    assert {r["state"] for r in runs.values()} == {"DONE"}


@needs_room_a
def test_a_packet_can_be_named_by_id_even_when_the_index_is_gone(capsys, tmp_path, fake_env, settings):
    packet_id = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))["packet_id"]
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    assert code == cli.EXIT_OK, err
    code, out, err = plate(
        capsys, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packet", packet_id, "--approve", "auto",
        env=fake_env,
    )  # fmt: skip
    assert code == cli.EXIT_OK, err
    assert "ingested from" not in out  # nothing was ingested again: the project already knew the packet
    for f in (settings.home / "index").glob("*.sqlite*"):
        f.unlink()
    code, out, err = plate(
        capsys, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packet", packet_id, "--approve", "auto",
        env=fake_env,
    )  # fmt: skip
    assert code == cli.EXIT_OK, err  # packets.json in the folder rebuilt the index
    assert sorted(r["attempt"] for r in project_runs(tmp_path).values())[-1] >= 1
    code, out, err = plate(
        capsys, "run", NIGHT, "--project", str(tmp_path / "proj"), "--packet", "pkt_UNKNOWN", env=fake_env
    )
    assert code == cli.EXIT_USAGE and "neither a packet folder nor an ingested packet" in err


@needs_room_a
def test_best_of_n_runs_a_group_under_one_quote(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto", "--n", "3")
    assert code == cli.EXIT_OK, (out, err)
    assert "3 x = 0.1200 USD" in out and out.count(" DONE") == 3
    runs = project_runs(tmp_path)
    assert len(runs) == 3
    assert len({json.loads(r["quote_json"])["sibling_group"] for r in runs.values()}) == 1


@needs_room_a
def test_human_approval_waits_for_y_and_n_sends_nothing(capsys, tmp_path, fake_env):
    asked: list[str] = []

    def no(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    code, out, err = run_recipe(capsys, tmp_path, fake_env, ask=no)
    assert code == cli.EXIT_REFUSED and "not approved" in err
    assert len(asked) == 1 and "0.0400 USD" in asked[0]
    (run,) = project_runs(tmp_path).values()
    assert run["state"] == "CANCELLED" and run["provider_job_id"] is None
    code, out, err = run_recipe(capsys, tmp_path, fake_env, ask=lambda _p: "y")
    assert code == cli.EXIT_OK, (out, err)
    assert sorted(r["state"] for r in project_runs(tmp_path).values()) == ["CANCELLED", "DONE"]


@needs_room_a
def test_human_approval_with_no_answer_is_a_no(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, ask=lambda _p: "")
    assert code == cli.EXIT_REFUSED


@needs_room_a
def test_an_envelope_is_a_hard_ceiling(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto", "--envelope", "0.01")
    assert code == cli.EXIT_REFUSED and "exceeds envelope" in err
    (run,) = project_runs(tmp_path).values()
    assert run["state"] == "CANCELLED"
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto", "--envelope", "1")
    assert code == cli.EXIT_OK, (out, err)


@needs_room_a
def test_auto_approval_obeys_the_same_cap_as_the_daemon(capsys, tmp_path, fake_env, monkeypatch):
    from plate_daemon import budgets

    original = cli.Session.pipeline

    def tight(self, engine=None):
        p = original(self, engine)
        p.policy = budgets.Policy(per_run_auto_cap_usd=0.05)  # a group of two costs 0.08
        return p

    monkeypatch.setattr(cli.Session, "pipeline", tight)
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto", "--n", "2")
    assert code == cli.EXIT_REFUSED and "auto-approve cap" in err
    assert {r["state"] for r in project_runs(tmp_path).values()} == {"CANCELLED"}


@needs_room_a
def test_a_vendor_engine_needs_plate_live(capsys, tmp_path):
    code, out, err = run_recipe(capsys, tmp_path, env_with(), "--approve", "auto")
    assert code == cli.EXIT_REFUSED and "PLATE_LIVE=1" in err
    assert not (tmp_path / "proj").exists()  # refused before anything was made or sent


@needs_room_a
def test_the_fake_engine_is_not_a_vendor(capsys, tmp_path):
    code, out, err = run_recipe(capsys, tmp_path, env_with(), "--engine", "fake", "--approve", "auto")
    assert code == cli.EXIT_USAGE and "PLATE_FAKE_ENGINE" in err


def test_motion_recipes_run_from_the_motion_node(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, recipe="croppair_pushin")
    assert code == cli.EXIT_USAGE and "Motion node" in err


def test_unknown_recipe_and_packet_are_usage_errors(capsys, tmp_path, fake_env):
    code, out, err = plate(
        capsys, "run", "no_such_recipe", "--project", str(tmp_path / "p"), "--packet", "x", env=fake_env
    )
    assert code == cli.EXIT_USAGE
    code, out, err = plate(
        capsys,
        "run",
        NIGHT,
        "--project",
        str(tmp_path / "p"),
        "--packet",
        str(tmp_path / "nope"),
        env=fake_env,
    )
    assert code == cli.EXIT_USAGE and "pass --packet <folder>" in err


@needs_room_a
def test_a_gate_failure_ends_the_run_with_exit_2(capsys, tmp_path):
    code, out, err = run_recipe(capsys, tmp_path, env_with(PLATE_FAKE_ENGINE=NOISE), "--approve", "auto")
    assert code == cli.EXIT_GATE_FAIL, (out, err)
    assert "GATE_FAIL" in out and "gates fail" in out and "--override-reason" in out


@needs_room_a
def test_the_rephotograph_recipe_only_runs_on_close_shots(capsys, tmp_path, fake_env):
    recipe = "sunburst_rephotograph_close"
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--shot-class", "wide", recipe=recipe)
    assert code == cli.EXIT_USAGE and "only runs on 'close' shots" in err
    code, out, err = run_recipe(
        capsys, tmp_path, fake_env, "--approve", "auto", "--shot-class", "close", recipe=recipe
    )
    assert code == cli.EXIT_OK, (out, err)
    assert "router (advisory)" in out and "mask: the whole frame" in out


@needs_room_a
def test_a_new_project_gets_no_d_grade_by_default_and_nda_gets_c0_c1(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    custody = json.loads((tmp_path / "proj" / "project.json").read_text(encoding="utf-8"))["custody"]
    assert custody["allowed_grades"] == ["C0", "C1", "C2"] and custody["nda"] is False
    other = env_with(PLATE_FAKE_ENGINE="1", PLATE_HOME=str(tmp_path / "home2"))
    code, out, err = plate(
        capsys, "run", NIGHT, "--project", str(tmp_path / "nda"), "--packet", str(ROOM_A), "--nda",
        "--approve", "auto", env=other,
    )  # fmt: skip
    assert code == cli.EXIT_OK, err
    custody = json.loads((tmp_path / "nda" / "project.json").read_text(encoding="utf-8"))["custody"]
    assert custody == {"nda": True, "allowed_grades": ["C0", "C1"]}


@needs_room_a
def test_a_project_id_cannot_be_given_a_second_folder(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    assert code == cli.EXIT_OK
    code, out, err = plate(
        capsys, "run", NIGHT, "--project", str(tmp_path / "second"), "--packet", str(ROOM_A), "--approve", "auto",
        env=fake_env,
    )  # fmt: skip
    assert code == cli.EXIT_USAGE and "already the project at" in err
    assert not (tmp_path / "second").exists()


# ---------------------------------------------------------------------------- plate gate


def write_png(path: Path, arr: np.ndarray) -> Path:
    assert cv2.imwrite(str(path), np.ascontiguousarray(arr[..., ::-1]))
    return path


def test_gate_scores_a_pair_and_writes_the_report(capsys, tmp_path):
    rng = np.random.default_rng(3)
    img = rng.integers(0, 255, (96, 160, 3), dtype=np.uint8)
    a, b = write_png(tmp_path / "a.png", img), write_png(tmp_path / "b.png", img)
    out_json = tmp_path / "report.json"
    code, out, err = plate(capsys, "gate", str(a), str(b), "--out", str(out_json))
    assert code == cli.EXIT_OK, err
    printed = json.loads(out)
    assert printed["verdict"] in ("pass", "advisory") and printed["tier"] == "C"
    assert json.loads(out_json.read_text(encoding="utf-8"))["verdict"] == printed["verdict"]
    assert "gate verdict" in err


def test_gate_never_resizes(capsys, tmp_path):
    a = write_png(tmp_path / "a.png", np.zeros((40, 60, 3), np.uint8))
    b = write_png(tmp_path / "b.png", np.zeros((40, 61, 3), np.uint8))
    code, out, err = plate(capsys, "gate", str(a), str(b))
    assert code == cli.EXIT_USAGE and "never resize" in err
    code, out, err = plate(capsys, "gate", str(a), str(tmp_path / "missing.png"))
    assert code == cli.EXIT_USAGE


@needs_room_a
def test_gate_with_a_packet_runs_the_full_set_and_fails_a_bad_candidate(capsys, tmp_path):
    plate_tif = ROOM_A / "display.tif"
    code, out, err = plate(capsys, "gate", str(plate_tif), str(plate_tif), "--packet", str(ROOM_A))
    assert code == cli.EXIT_OK, err
    report = json.loads(out)
    assert (
        report["tier"] == "A" and "gbuffer" in report["metrics"] and report["metrics"]["invention_flags"] == 0
    )
    noise = write_png(
        tmp_path / "noise.png", np.random.default_rng(0).integers(0, 255, (360, 640, 3), np.uint8)
    )
    code, out, err = plate(capsys, "gate", str(plate_tif), str(noise), "--packet", str(ROOM_A))
    assert code == cli.EXIT_GATE_FAIL
    assert json.loads(out)["verdict"] == "fail"


# ---------------------------------------------------------------------------- plate export


@needs_room_a
def test_export_writes_each_format_with_lineage_and_one_shared_gate_json(capsys, tmp_path, fake_env):
    import OpenEXR
    import tifffile

    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    rid = run_id_in(out)
    dest = tmp_path / "out"
    for fmt in ("tiff16", "png16", "exr"):
        code, out, err = plate(capsys, "export", rid, "--to", str(dest), "--format", fmt, env=fake_env)
        assert code == cli.EXIT_OK, (fmt, out, err)
    tif, png, exr = (dest / f"{rid}{ext}" for ext in (".tif", ".png", ".exr"))
    assert tifffile.imread(tif).dtype == np.uint16
    assert cv2.imread(str(png), cv2.IMREAD_UNCHANGED).dtype == np.uint16
    assert OpenEXR.File(str(exr)).header()["chromaticities"][0] == pytest.approx(0.64)
    for path in (tif, png, exr):
        lin = lineage.read(path)
        assert lin is not None and lin.run_id == rid and lin.project_id == "plate-fixture"
        assert lin.gate_report == f"gates/{rid}.1.json"
    report = json.loads((dest / f"{rid}.gate.json").read_text(encoding="utf-8"))
    assert report["verdict"] in ("pass", "advisory") and report["metric_code_hash"]
    assert (
        report["export"]["files"] == [tif.name, png.name, exr.name]
        and report["export"]["override_reason"] is None
    )
    # never overwrites an export
    code, out, err = plate(capsys, "export", rid, "--to", str(dest), "--format", "tiff16", env=fake_env)
    assert code == cli.EXIT_ERROR and "already exists" in err
    ledger = [
        json.loads(x)["kind"]
        for x in (tmp_path / "proj" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ledger.count("export") == 3


@needs_room_a
def test_export_refuses_a_failed_run_until_a_reason_is_written(capsys, tmp_path):
    env = env_with(PLATE_FAKE_ENGINE=NOISE)
    code, out, err = run_recipe(capsys, tmp_path, env, "--approve", "auto")
    assert code == cli.EXIT_GATE_FAIL
    rid = run_id_in(out, "GATE_FAIL")
    dest = tmp_path / "out"
    code, out, err = plate(capsys, "export", rid, "--to", str(dest), env=env)
    assert code == cli.EXIT_GATE_FAIL and "failed its gates" in err and not dest.exists()
    code, out, err = plate(
        capsys, "export", rid, "--to", str(dest), "--override-reason", "too short", env=env
    )
    assert code == cli.EXIT_GATE_FAIL and not dest.exists()
    reason = "client signed the soft sky off on the 17th"
    code, out, err = plate(capsys, "export", rid, "--to", str(dest), "--override-reason", reason, env=env)
    assert code == cli.EXIT_OK, err
    assert "GATE_FAIL shipped on override" in out
    block = json.loads((dest / f"{rid}.gate.json").read_text(encoding="utf-8"))
    assert block["verdict"] == "fail" and block["export"]["override_reason"] == reason
    # the override is on the run's own log and in the ledger, once
    with Index(Settings.load().home).project("plate-fixture") as conn:
        notes = [e.reason for e in run_events(conn, rid) if e.kind == "override"]
    assert notes == [f"override: {reason}"]
    kinds = [
        json.loads(x)["kind"]
        for x in (tmp_path / "proj" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert kinds.count("override") == 1 and kinds.count("export") == 1
    # a recorded override carries over: a second format needs no reason
    code, out, err = plate(capsys, "export", rid, "--to", str(dest), "--format", "png16", env=env)
    assert code == cli.EXIT_OK, err


def test_export_of_an_unknown_run_or_format_is_a_usage_error(capsys, tmp_path):
    code, out, err = plate(capsys, "export", "run_nope", "--to", str(tmp_path))
    assert code == cli.EXIT_USAGE and "--project" in err
    code, out, err = plate(capsys, "export", "run_x", "--to", str(tmp_path), "--format", "jpeg")
    assert code == cli.EXIT_USAGE


@needs_room_a
def test_export_names_are_windows_file_names(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    rid = run_id_in(out)
    for bad in ("a/b", "a:b", ".hidden", ""):
        if not bad:
            continue
        code, out, err = plate(
            capsys, "export", rid, "--to", str(tmp_path / "o"), "--name", bad, env=fake_env
        )
        assert code == cli.EXIT_USAGE, bad
    code, out, err = plate(
        capsys, "export", rid, "--to", str(tmp_path / "o"), "--name", "shot_01-v2", env=fake_env
    )
    assert code == cli.EXIT_OK and (tmp_path / "o" / "shot_01-v2.tif").is_file()


@needs_room_a
def test_export_finds_a_run_through_project_after_the_index_is_gone(capsys, tmp_path, fake_env, settings):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    rid = run_id_in(out)
    for f in (settings.home / "index").glob("*.sqlite*"):
        f.unlink()
    code, out, err = plate(capsys, "export", rid, "--to", str(tmp_path / "o"), env=fake_env)
    assert code == cli.EXIT_USAGE
    code, out, err = plate(
        capsys, "export", rid, "--to", str(tmp_path / "o"), "--project", str(tmp_path / "proj"), env=fake_env
    )
    assert code == cli.EXIT_OK, err


# ---------------------------------------------------------------------------- reindex, gc, pack


@needs_room_a
def test_reindex_rebuilds_the_index_from_the_folder(capsys, tmp_path, fake_env, settings):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    rid = run_id_in(out)
    for f in (settings.home / "index").glob("*.sqlite*"):
        f.unlink()
    code, out, err = plate(capsys, "reindex", str(tmp_path / "proj"), env=fake_env)
    assert code == cli.EXIT_OK, err
    assert "reindexed plate-fixture" in out and "runs 1" in out and "packets 1" in out
    assert Index(settings.home).locate_run(rid) == "plate-fixture"
    code, out, err = plate(capsys, "reindex", "plate-fixture", "--verify", env=fake_env)  # by id this time
    assert code == cli.EXIT_OK, err
    code, out, err = plate(capsys, "reindex", str(tmp_path / "not-a-project"), env=fake_env)
    assert code == cli.EXIT_USAGE


def plant(project: Path, key_char: str, size: int = 4096) -> Path:
    key = key_char * 64
    path = project / "assets" / key[:2] / f"{key}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@needs_room_a
def test_gc_deletes_only_what_nothing_references(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    rid = run_id_in(out)
    proj = tmp_path / "proj"
    cas = CAS(proj / "assets")
    before = {k for k, _e, _p in cas.iter_objects()}
    assert len(before) >= 4  # raw result, composite, staged PNG, staged mask
    stray = plant(proj, "a")
    # dry run: names what it would delete, deletes nothing
    code, out, err = plate(capsys, "gc", str(proj), "--dry-run", "--min-age-hours", "0", env=fake_env)
    assert code == cli.EXIT_OK and "would delete 1 unreferenced" in out and stray.is_file()
    # the grace period keeps a fresh unreferenced blob
    code, out, err = plate(capsys, "gc", str(proj), env=fake_env)
    assert "deleted 0" in out and "younger than 1 h" in out and stray.is_file()
    code, out, err = plate(capsys, "gc", str(proj), "--min-age-hours", "0", env=fake_env)
    assert code == cli.EXIT_OK and "deleted 1 unreferenced" in out
    assert not stray.exists() and not stray.parent.exists()
    after = {k for k, _e, _p in cas.iter_objects()}
    assert after == before and all(cas.verify(k) for k in after)
    # the run still exports: its output survived
    code, out, err = plate(capsys, "export", rid, "--to", str(tmp_path / "o"), env=fake_env)
    assert code == cli.EXIT_OK, err


@needs_room_a
def test_gc_keeps_what_the_graph_and_ledger_point_at(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(
        capsys,
        tmp_path,
        fake_env,
        "--approve",
        "auto",
        "--shot-class",
        "close",
        recipe="sunburst_rephotograph_close",
    )
    assert code == cli.EXIT_OK, err
    proj = tmp_path / "proj"
    cas = CAS(proj / "assets")
    before = {k for k, _e, _p in cas.iter_objects()}
    counts = cas.refcount(Project.load(proj))
    assert any(n > 0 for n in counts.values())  # the full-frame mask is in project.json's Mask node
    code, out, err = plate(capsys, "gc", str(proj), "--min-age-hours", "0", env=fake_env)
    assert code == cli.EXIT_OK and "deleted 0" in out
    assert {k for k, _e, _p in cas.iter_objects()} == before


@needs_room_a
def test_pack_leaves_out_big_blobs_unless_full(capsys, tmp_path, fake_env):
    code, out, err = run_recipe(capsys, tmp_path, fake_env, "--approve", "auto")
    proj = tmp_path / "proj"
    big = plant(proj, "b", size=2 * 1024 * 1024)
    zip_path = tmp_path / "proj.zip"
    code, out, err = plate(
        capsys, "pack", str(proj), "--out", str(zip_path), "--max-blob-mb", "1", env=fake_env
    )
    assert code == cli.EXIT_OK, err
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("pack_manifest.json"))
    assert {"project.json", "packets.json", "runs.jsonl", "pack_manifest.json"} <= names
    assert any(n.startswith("gates/") for n in names)
    assert f"assets/{big.parent.name}/{big.name}" not in names
    assert [o["path"] for o in manifest["omitted_blobs"]] == [f"assets/{big.parent.name}/{big.name}"]
    assert not any(n.endswith(".lock") or ".tmp" in n for n in names)
    assert "left out" in out
    code, out, err = plate(capsys, "pack", str(proj), "--out", str(zip_path), env=fake_env)
    assert code == cli.EXIT_USAGE and "never overwrites" in err
    full = tmp_path / "full.zip"
    code, out, err = plate(
        capsys, "pack", str(proj), "--out", str(full), "--full", "--max-blob-mb", "1", env=fake_env
    )
    assert code == cli.EXIT_OK
    with zipfile.ZipFile(full) as z:
        assert f"assets/{big.parent.name}/{big.name}" in z.namelist()
        assert json.loads(z.read("pack_manifest.json"))["omitted_blobs"] == []


# ---------------------------------------------------------------------------- plate smoke


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the fixture smoke opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_fixture_smoke_replays_every_recorded_adapter_without_a_network(capsys, no_network, fake_env):
    code, out, err = plate(capsys, "smoke", env=fake_env)
    assert code == cli.EXIT_OK, (out, err)
    last = out.strip().splitlines()[-1]
    m = re.fullmatch(r"fixture smoke: (\d+) adapters ok", last)
    assert m and int(m.group(1)) >= 10, out
    for engine in ("nano-banana-pro via gemini", "gpt-image-2.5 via openai", "flux-3-image via bfl",
                   "seedream-5-pro via atlas", "kling-o3-4k via atlas", "veo-3.1 via vertex",
                   "seedance-2.5-i2v via byteplus", "seedance-2.5-i2v via wavespeed"):  # fmt: skip
        assert f"{engine}: ok" in out, engine
    assert "local route" in out and "no adapter speaks this route" in out  # skips say why


def test_fixture_smoke_restores_the_environment(capsys, monkeypatch, fake_env):
    for name in ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "ATLASCLOUD_KEY"):
        monkeypatch.delenv(name, raising=False)
    plate(capsys, "smoke", "--engines", "nano-banana-pro,veo-3.1", env=fake_env)
    assert not any(k in os.environ for k in ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "VERTEX_ACCESS_TOKEN"))


def test_fixture_smoke_can_be_narrowed_and_reports(capsys, no_network, tmp_path):
    report = tmp_path / "smoke.json"
    code, out, err = plate(capsys, "smoke", "--engines", "flux-3-image", "--out", str(report))
    assert code == cli.EXIT_OK and out.strip().splitlines()[-1] == "fixture smoke: 1 adapters ok"
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["mode"] == "fixture" and [r["engine_id"] for r in doc["rows"]] == ["flux-3-image"]
    code, out, err = plate(capsys, "smoke", "--engines", "no-such-engine")
    assert code == cli.EXIT_USAGE


def test_smoke_uses_the_cheapest_legal_request():
    from plate_engines.loader import load_manifests

    m = load_manifests()
    assert cli.min_cost_params(m["seedance-2.5-i2v"]) == {"duration": 4, "resolution": "480P"}
    assert cli.min_cost_params(m["veo-3.1"]) == {"duration": 4, "resolution": "720p"}
    assert cli.min_cost_params(m["kling-o3-4k"]) == {
        "duration": 1
    }  # no default resolution: the 4K slug stays
    assert cli.min_cost_params(m["gpt-image-2.5"]) == {"quality": "low"}
    assert cli.min_cost_params(m["nano-banana-pro"]) == {"image_size": "1K"}
    assert cli.min_cost_params(m["seedream-5-pro"]) == {"resolution": "1K"}


def test_replay_transport_never_falls_through(no_network):
    from plate_engines.adapters.transport import UnrecordedRequest, make_client

    replay = cli.ReplayTransport(("gemini",), ("edit_a",))
    client = make_client(replay)
    with pytest.raises(UnrecordedRequest):
        client.get("https://example.invalid/v1/anything")
    with pytest.raises(UnrecordedRequest):
        client.post("https://generativelanguage.googleapis.com/v1beta/models/other:generateContent", json={})


def test_live_smoke_needs_plate_live_and_an_explicit_budget(capsys, monkeypatch):
    seen: list[bool] = []

    def factory(case, manifest, route, live):
        seen.append(live)
        raise AssertionError("no transport may be built before the refusals")

    code, out, err = plate(capsys, "smoke", "--live", "--budget-usd", "5", transport_factory=factory)
    assert code == cli.EXIT_REFUSED and "PLATE_LIVE=1" in err
    code, out, err = plate(capsys, "smoke", "--live", env=env_with(PLATE_LIVE="1"), transport_factory=factory)
    assert code == cli.EXIT_REFUSED and "--budget-usd" in err
    code, out, err = plate(
        capsys,
        "smoke",
        "--live",
        "--budget-usd",
        "0",
        env=env_with(PLATE_LIVE="1"),
        transport_factory=factory,
    )
    assert code == cli.EXIT_REFUSED
    assert seen == []


def live_env(monkeypatch) -> dict[str, str]:
    keys = {
        "GEMINI_API_KEY": "k", "OPENAI_API_KEY": "k", "BFL_API_KEY": "k", "ATLASCLOUD_KEY": "k",
        "ARK_API_KEY": "k", "WAVESPEED_API_KEY": "k", "VERTEX_ACCESS_TOKEN": "t", "GOOGLE_CLOUD_PROJECT": "plate-test",
    }  # fmt: skip
    for k, v in keys.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PLATE_LIVE", "1")
    return env_with(**{k: v for k, v in os.environ.items() if k in keys}, PLATE_LIVE="1")


def test_live_smoke_prints_custody_before_each_send_and_stops_at_the_budget(capsys, monkeypatch, no_network):
    env = live_env(monkeypatch)
    built: dict[str, cli.ReplayTransport] = {}
    lives: list[bool] = []

    def factory(case, manifest, route, live):
        lives.append(live)
        built[manifest.id] = cli.ReplayTransport(case.contract, case.prefer)  # stands in for the network
        return built[manifest.id]

    # flux 0.12 + gpt-image 0.16 = 0.28; kling-o3-4k (0.357) would pass 0.5
    code, out, err = plate(
        capsys, "smoke", "--live", "--budget-usd", "0.5", env=env, transport_factory=factory
    )
    assert code == cli.EXIT_REFUSED, (out, err)
    assert set(lives) == {True}
    assert built["flux-3-image"].served and built["gpt-image-2.5"].served
    assert built["kling-o3-4k"].served == []  # quoted, refused on the budget, nothing sent
    assert set(built) == {
        "flux-3-image",
        "gpt-image-2.5",
        "kling-o3-4k",
    }  # and the engines after it never started
    lines = out.splitlines()
    for engine in ("flux-3-image", "gpt-image-2.5"):
        head = next(i for i, ln in enumerate(lines) if ln.startswith(f"{engine} via") and "custody D" in ln)
        assert "quote" in lines[head] and "of 0.5000 USD" in lines[head]
        assert lines[head + 1].strip() == "sending ..."  # custody and price come before the send
        assert any(ln.startswith(f"{engine} via") and ": ok" in ln for ln in lines[head + 2 :])
    assert "stopping: this call would take the running total past the budget" in out
    assert "not run: the budget was reached" in out
    assert (
        out.strip()
        .splitlines()[-1]
        .startswith("live smoke: 2 adapters ok, 0 failed; quoted total 0.2800 USD")
    )


def test_live_smoke_with_room_runs_the_chosen_engines_and_skips_missing_keys(capsys, monkeypatch, no_network):
    env = live_env(monkeypatch)
    monkeypatch.delenv("BFL_API_KEY")
    env.pop("BFL_API_KEY")
    code, out, err = plate(
        capsys, "smoke", "--live", "--budget-usd", "5", "--engines", "flux-3-image,gpt-image-2.5", env=env,
        transport_factory=lambda case, manifest, route, live: cli.ReplayTransport(case.contract, case.prefer),
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    assert "flux-3-image via bfl: skipped (set BFL_API_KEY)" in out
    assert "gpt-image-2.5 via openai: ok" in out and "live smoke: 1 adapters ok" in out


def test_live_smoke_never_sends_a_placeholder_model_id(capsys, monkeypatch, no_network):
    env = live_env(monkeypatch)
    built: list[cli.ReplayTransport] = []

    def factory(case, manifest, route, live):
        built.append(cli.ReplayTransport(case.contract, case.prefer))
        return built[-1]

    code, out, err = plate(
        capsys,
        "smoke",
        "--live",
        "--budget-usd",
        "5",
        "--engines",
        "seedream-5-pro",
        env=env,
        transport_factory=factory,
    )
    # the fixtures fill in the Seedream slug; a live call refuses to guess it, and nothing is sent
    assert code == cli.EXIT_OK, (out, err)
    assert "seedream-5-pro via atlas: skipped (no vendor model id yet" in out
    assert built and all(t.served == [] for t in built)
    assert out.count("seedream-5-pro via atlas") == 1  # the row is printed once


# ---------------------------------------------------------------------------- entry points


def test_usage_errors_exit_4_and_help_exits_0(capsys):
    assert plate(capsys, "run")[0] == cli.EXIT_USAGE
    assert plate(capsys, "frobnicate")[0] == cli.EXIT_USAGE
    assert plate(capsys, "--help")[0] == cli.EXIT_OK
    assert plate(capsys, "--version")[0] == cli.EXIT_OK
    code, out, err = plate(capsys, "run", NIGHT, "--project", "p", "--packet", "x", "--approve", "maybe")
    assert code == cli.EXIT_USAGE


def test_pyproject_exposes_the_plate_console_script():
    doc = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert doc["project"]["scripts"]["plate"] == "plate_daemon.cli:main"
    module, _, attr = doc["project"]["scripts"]["plate"].partition(":")
    assert module == cli.__name__ and callable(getattr(cli, attr))


def test_shims_are_there_and_windows_safe():
    sh = ROOT / "scripts" / "plate"
    cmd = ROOT / "scripts" / "plate.cmd"
    assert sh.read_bytes().startswith(b"#!/usr/bin/env bash\n") and b"\r" not in sh.read_bytes()
    if os.name != "nt":
        assert os.access(sh, os.X_OK)
    raw = cmd.read_bytes()
    raw.decode("ascii")  # cmd.exe reads the OEM code page: ASCII only
    assert raw.count(b"\r\n") == raw.count(b"\n") and raw.endswith(b"\r\n")
    assert b"plate_daemon.cli" in raw and b"PYTHONUTF8" in raw and b"%*" in raw


def test_python_dash_m_runs_the_smoke_with_the_done_when_environment(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "plate_daemon.cli", "smoke", "--engines", "flux-3-image,veo-3.1"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=300, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    # flux, veo via vertex and veo via runway
    assert proc.stdout.strip().splitlines()[-1] == "fixture smoke: 3 adapters ok"


@pytest.mark.skipif(os.name == "nt", reason="the bash shim is the Mac entry; plate.cmd is its Windows twin")
def test_the_bash_shim_keeps_the_callers_directory(tmp_path):
    env = env_with(PLATE_HOME=str(tmp_path / "home"))
    proc = subprocess.run(
        [str(ROOT / "scripts" / "plate"), "gate", "a.png", "b.png"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp_path, timeout=300, check=False,
    )  # fmt: skip
    assert (
        proc.returncode == cli.EXIT_USAGE and "a.png" in proc.stderr
    )  # relative to tmp_path, which has no a.png


# ---------------------------------------------------------------------------- small pieces


def test_param_flags_are_json_or_text():
    assert cli._parse_params(["seed=7", "quality=high", 'box={"x": 1}', "on=true"]) == {
        "seed": 7,
        "quality": "high",
        "box": {"x": 1},
        "on": True,
    }
    with pytest.raises(cli.CliExit) as e:
        cli._parse_params(["novalue"])
    assert e.value.code == cli.EXIT_USAGE


def test_export_rejects_what_it_cannot_write_before_touching_anything(tmp_path):
    from plate_daemon import export as export_mod

    assert set(export_mod.FORMATS) == {"tiff16", "png16", "exr"}
    with pytest.raises(ValueError, match="unknown format"):
        export_mod.export(None, "jpeg", tmp_path, conn=None, cas=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Windows file name"):
        export_mod.export(None, "tiff16", tmp_path, conn=None, cas=None, name="a:b")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []
