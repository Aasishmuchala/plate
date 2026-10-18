"""Wave 12 / T143: the CLI's export orders its override before its bytes, number options are bounded, and
`--to` is checked before anything is recorded (stress findings `cli-sidecar-reason-unledgered` and
`cli-envelope-edges`).

Everything runs in process on the daemon's own `FakeEngine` under the per-test PLATE_HOME: a GATE_FAIL run is
`PLATE_FAKE_ENGINE={"echo": false, "size": [640, 360]}` (noise) on the committed `room_a` packet. No socket, no
client data. Usage errors are exit 4 (`EXIT_USAGE`): in this CLI exit 2 is a gate failure, so the plan's
"exit 2" for a bad argument is the usage code here.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plate_daemon import cli
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.export import Exported, recorded_override
from plate_daemon.gate_reports import Service as ReportService
from plate_daemon.index import Index
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import Pipeline, PipelineError

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
NIGHT = "night_relight_changelist"
NOISE = json.dumps({"echo": False, "size": [640, 360]})
PROJECT = "plate-fixture"
needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)

SCRUBBED = ("PLATE_LIVE", "PLATE_FAKE_ENGINE", "PLATE_ROUTER_MODE", "PLATE_DEBUG")


def env_with(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if k not in SCRUBBED}, **extra}


def plate(capsys, *argv: str, env: dict[str, str] | None = None, **kw) -> tuple[int, str, str]:
    code = cli.main(list(argv), env=env if env is not None else env_with(), **kw)
    out, err = capsys.readouterr()
    return code, out, err


def gate_fail_run(capsys, tmp_path: Path) -> tuple[str, dict[str, str]]:
    """A GATE_FAIL run in `tmp_path/proj` (the project id is `plate-fixture`) and the env that made it."""
    env = env_with(PLATE_FAKE_ENGINE=NOISE)
    code, out, err = plate(
        capsys,
        "run",
        NIGHT,
        "--project",
        str(tmp_path / "proj"),
        "--packet",
        str(ROOM_A),
        "--approve",
        "auto",
        env=env,
    )
    assert code == cli.EXIT_GATE_FAIL, (out, err)
    m = re.search(r"^run (run_\w+) GATE_FAIL\b", out, re.M)
    assert m, out
    return m.group(1), env


def records(tmp_path: Path, kind: str, run_id: str) -> list[dict]:
    led = Ledger(tmp_path / "proj")
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def override_over_http(settings: Settings, run_id: str, reason: str) -> None:
    auth = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings), base_url="http://127.0.0.1:4610") as c:
        r = c.post(f"/api/v0/runs/{run_id}/override", json={"reason": reason, "by": "human"}, headers=auth)
        assert r.status_code == 200, r.text


def run_notes(run_id: str) -> list[str | None]:
    from plate_daemon.machine import run_events

    with Index(Settings.load().home).project(PROJECT) as conn:
        return [e.reason for e in run_events(conn, run_id) if e.kind == "override"]


# ---------------------------------------------------------------------------- the override goes first


@needs_room_a
def test_a_typed_reason_that_differs_from_the_recorded_one_is_ledgered_before_the_export(
    capsys, tmp_path, settings
):
    rid, env = gate_fail_run(capsys, tmp_path)
    reason_a = "reason A: the art director approved the soft sky"
    reason_b = "reason B: typed at the command line instead"
    override_over_http(settings, rid, reason_a)
    out_dir = tmp_path / "out"
    code, out, err = plate(
        capsys, "export", rid, "--to", str(out_dir), "--override-reason", reason_b, env=env
    )
    assert code == cli.EXIT_OK, (out, err)
    overrides = records(tmp_path, "override", rid)
    assert [o["reason"] for o in overrides] == [reason_a, reason_b]
    (exported,) = records(tmp_path, "export", rid)
    assert overrides[1]["seq"] < exported["seq"]  # B was on the record before the export record
    sidecar = json.loads((out_dir / f"{rid}.gate.json").read_text(encoding="utf-8"))
    assert sidecar["export"]["override_reason"] == reason_b
    assert run_notes(rid) == [f"override: {reason_a}", f"override: {reason_b}"]
    assert "GATE_FAIL shipped on override" in out and reason_b in out


@needs_room_a
def test_the_override_is_on_the_record_when_export_is_entered(capsys, tmp_path, monkeypatch):
    rid, env = gate_fail_run(capsys, tmp_path)
    reason = "client signed the soft sky off on the 17th"
    seen: dict[str, object] = {}
    real = cli.export

    def spy(run, fmt, out_dir, override=None, *, conn, **kw):
        seen["note"] = recorded_override(conn, run.id)
        seen["ledgered"] = [r["reason"] for r in records(tmp_path, "override", run.id)]
        return real(run, fmt, out_dir, override, conn=conn, **kw)

    monkeypatch.setattr(cli, "export", spy)
    code, out, err = plate(
        capsys, "export", rid, "--to", str(tmp_path / "out"), "--override-reason", reason, env=env
    )
    assert code == cli.EXIT_OK, (out, err)
    assert seen == {"note": reason, "ledgered": [reason]}


@needs_room_a
def test_a_ledger_that_refuses_the_override_stops_the_export(capsys, tmp_path, monkeypatch):
    rid, env = gate_fail_run(capsys, tmp_path)
    real = Pipeline.ledger

    def refuse(self, project_id, kind, run, **kw):
        if kind == "override":
            raise PipelineError(
                "the project ledger is unavailable (OSError: disk)", code="ledger_unavailable", status=503
            )
        return real(self, project_id, kind, run, **kw)

    monkeypatch.setattr(Pipeline, "ledger", refuse)
    out_dir = tmp_path / "out"
    code, out, err = plate(
        capsys,
        "export",
        rid,
        "--to",
        str(out_dir),
        "--override-reason",
        "reason that cannot be written down",
        env=env,
    )
    assert code == cli.EXIT_REFUSED, (out, err)
    assert "ledger unavailable: nothing exported" in err
    assert not out_dir.exists()
    assert records(tmp_path, "override", rid) == [] and records(tmp_path, "export", rid) == []
    assert run_notes(rid) == []  # no note behind it either: a retry starts clean
    monkeypatch.setattr(Pipeline, "ledger", real)
    code, out, err = plate(
        capsys,
        "export",
        rid,
        "--to",
        str(out_dir),
        "--override-reason",
        "reason that cannot be written down",
        env=env,
    )
    assert code == cli.EXIT_OK, (out, err)
    assert len(records(tmp_path, "override", rid)) == 1


@needs_room_a
def test_a_reason_too_short_to_export_is_not_recorded(capsys, tmp_path):
    rid, env = gate_fail_run(capsys, tmp_path)
    out_dir = tmp_path / "out"
    code, out, err = plate(
        capsys, "export", rid, "--to", str(out_dir), "--override-reason", "too short", env=env
    )
    assert code == cli.EXIT_GATE_FAIL and not out_dir.exists()
    assert records(tmp_path, "override", rid) == [] and run_notes(rid) == []


@needs_room_a
def test_the_reason_already_on_record_is_not_ledgered_again(capsys, tmp_path, settings):
    rid, env = gate_fail_run(capsys, tmp_path)
    reason = "the client approved the drifted frame"
    override_over_http(settings, rid, reason)
    code, out, err = plate(
        capsys, "export", rid, "--to", str(tmp_path / "out"), "--override-reason", reason, env=env
    )
    assert code == cli.EXIT_OK, (out, err)
    assert [o["reason"] for o in records(tmp_path, "override", rid)] == [reason]
    assert run_notes(rid) == [f"override: {reason}"]


@needs_room_a
def test_a_done_run_ignores_a_typed_reason(capsys, tmp_path):
    env = env_with(PLATE_FAKE_ENGINE="1")
    code, out, err = plate(
        capsys,
        "run",
        NIGHT,
        "--project",
        str(tmp_path / "proj"),
        "--packet",
        str(ROOM_A),
        "--approve",
        "auto",
        env=env,
    )
    assert code == cli.EXIT_OK, (out, err)
    rid = re.search(r"^run (run_\w+) DONE\b", out, re.M).group(1)  # type: ignore[union-attr]
    code, out, err = plate(
        capsys,
        "export",
        rid,
        "--to",
        str(tmp_path / "out"),
        "--override-reason",
        "typed on a take that needs none",
        env=env,
    )
    assert code == cli.EXIT_OK, (out, err)
    assert records(tmp_path, "override", rid) == []
    # the report version the sidecar copied is marked shipped, as the daemon's export route does
    svc = ReportService(tmp_path / "proj")
    assert svc.latest(rid).shipped is True


# ---------------------------------------------------------------------------- a repeat export


def _as_repeat(done: Exported) -> SimpleNamespace:
    return SimpleNamespace(**dataclasses.asdict(done) | {"framing": done.framing}, repeat=True)


@needs_room_a
def test_a_repeat_export_prints_that_and_exits_0_after_the_ledger_record(capsys, tmp_path, monkeypatch):
    env = env_with(PLATE_FAKE_ENGINE="1")
    code, out, err = plate(
        capsys,
        "run",
        NIGHT,
        "--project",
        str(tmp_path / "proj"),
        "--packet",
        str(ROOM_A),
        "--approve",
        "auto",
        env=env,
    )
    assert code == cli.EXIT_OK, (out, err)
    rid = re.search(r"^run (run_\w+) DONE\b", out, re.M).group(1)  # type: ignore[union-attr]
    out_dir = tmp_path / "out"
    real = cli.export
    first: dict[str, Exported] = {}

    def remember(*a, **kw):  # T127's contract: the second export of identical bytes answers repeat=True
        if "done" not in first:
            first["done"] = real(*a, **kw)
            return first["done"]
        return _as_repeat(first["done"])

    monkeypatch.setattr(cli, "export", remember)
    code, out, err = plate(capsys, "export", rid, "--to", str(out_dir), env=env)
    assert code == cli.EXIT_OK and f"exported {out_dir / (rid + '.tif')}" in out, (out, err)
    code, out, err = plate(capsys, "export", rid, "--to", str(out_dir), env=env)
    assert code == cli.EXIT_OK, (out, err)
    assert out.strip() == f"already exported (identical): {out_dir / (rid + '.tif')}"
    one, two = records(tmp_path, "export", rid)
    assert "repeat" not in one["data"] and two["data"]["repeat"] is True
    assert ReportService(tmp_path / "proj").latest(rid).shipped is True


@needs_room_a
@pytest.mark.skipif(
    "repeat" not in Exported.__dataclass_fields__, reason="T127's Exported.repeat has not landed yet"
)
def test_a_real_repeat_export_is_exit_0(capsys, tmp_path):
    env = env_with(PLATE_FAKE_ENGINE="1")
    code, out, err = plate(
        capsys,
        "run",
        NIGHT,
        "--project",
        str(tmp_path / "proj"),
        "--packet",
        str(ROOM_A),
        "--approve",
        "auto",
        env=env,
    )
    rid = re.search(r"^run (run_\w+) DONE\b", out, re.M).group(1)  # type: ignore[union-attr]
    out_dir = tmp_path / "out"
    assert plate(capsys, "export", rid, "--to", str(out_dir), env=env)[0] == cli.EXIT_OK
    code, out, err = plate(capsys, "export", rid, "--to", str(out_dir), env=env)
    assert code == cli.EXIT_OK and out.startswith("already exported (identical): "), (out, err)
    assert [r["data"].get("repeat") for r in records(tmp_path, "export", rid)] == [None, True]


# ---------------------------------------------------------------------------- number options


@pytest.mark.parametrize("text", ["inf", "-inf", "nan", "1e400", "-1e400", "Infinity", "NaN"])
def test_finite_float_refuses_what_is_not_finite(text):
    parse = cli.finite_float(0.0, 10.0, "thing")
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        parse(text)


def test_finite_float_checks_the_range_and_the_spelling():
    parse = cli.finite_float(0.0, 10.0, "thing")
    assert parse("0") == 0.0 and parse("10") == 10.0 and parse("2.5") == 2.5 and parse("1e1") == 10.0
    for bad in ("-0.001", "10.01", "-5", "1e300"):
        with pytest.raises(argparse.ArgumentTypeError, match=r"thing .* out of range: use 0 to 10"):
            parse(bad)
    for bad in ("abc", "", "1,5", "0x10"):
        with pytest.raises(argparse.ArgumentTypeError, match="not a number"):
            parse(bad)
    assert cli.finite_float(0.0, 1.0)("2.0".replace("2", "0")) == 0.0  # `what` is optional


NUMBER_CASES = [
    ["run", NIGHT, "--project", "p", "--packet", "x", "--envelope"],
    ["gc", "p", "--min-age-hours"],
    ["pack", "p", "--out", "z.zip", "--max-blob-mb"],
    ["smoke", "--live", "--budget-usd"],
    ["import", "f.png", "--project", "p", "--packet", "x", "--cost-usd"],
    ["estimate", "--engine", "fake", "--task", "relight", "--shot-class", "wide", "--seconds"],
]


@pytest.mark.parametrize("head", NUMBER_CASES, ids=lambda h: h[-1])
@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "1e400", "-1", "1e300"])
def test_every_number_option_refuses_inf_nan_and_out_of_range(capsys, head, value):
    code, out, err = plate(
        capsys, *head[:-1], f"{head[-1]}={value}"
    )  # `=`: argparse reads a bare -inf as a flag
    assert code == cli.EXIT_USAGE, (head, value, out, err)
    assert "Traceback" not in err and ("finite" in err or "out of range" in err), err


def test_in_range_numbers_still_parse():
    parser = cli.build_parser()
    args = parser.parse_args(["gc", "p", "--min-age-hours", "0"])
    assert args.min_age_hours == 0.0
    args = parser.parse_args(["pack", "p", "--out", "z", "--max-blob-mb", "1.5"])
    assert args.max_blob_mb == 1.5
    args = parser.parse_args(["run", NIGHT, "--project", "p", "--envelope", "0.05"])
    assert args.envelope == 0.05
    args = parser.parse_args(["smoke", "--live", "--budget-usd", "5"])
    assert args.budget_usd == 5.0
    args = parser.parse_args(["gc", "p"])
    assert args.min_age_hours == cli.GC_GRACE_HOURS


def test_a_float_option_cannot_be_added_without_the_bound():
    """Every `type=float` in the parser is gone: a new number option goes through `finite_float`."""
    source = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert "type=float" not in source


# ---------------------------------------------------------------------------- --to


def test_to_naming_a_file_is_a_usage_error_with_the_path(capsys, tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("hi", encoding="utf-8")
    for argv in (
        ["export", "run_x", "--to", str(afile)],
        ["export", "--runs", "run_x,run_y", "--to", str(afile)],
        ["export", "--accepted", "--project", "p", "--to", str(afile)],
        ["deliver", "run_x", "--format", "prores4444", "--to", str(afile)],
    ):
        code, out, err = plate(capsys, *argv)
        assert code == cli.EXIT_USAGE, (argv, out, err)
        assert f"--to must be a folder, got a file: {afile}" in err, err
    assert afile.read_text(encoding="utf-8") == "hi"


def test_to_under_a_file_cannot_be_written(capsys, tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("hi", encoding="utf-8")
    code, out, err = plate(capsys, "export", "run_x", "--to", str(afile / "sub"))
    assert code == cli.EXIT_USAGE and f"cannot write to {afile / 'sub'}: " in err, err
    assert "Not a directory" in err or "not a directory" in err.lower()
    assert "Traceback" not in err and "FileExistsError" not in err


@pytest.mark.skipif(os.name == "nt", reason="a read-only folder is a POSIX permission bit")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_to_is_a_usage_error_and_creates_nothing(capsys, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        for target in (locked, locked / "new" / "deeper"):
            code, out, err = plate(capsys, "export", "run_x", "--to", str(target))
            assert code == cli.EXIT_USAGE, (target, out, err)
            assert f"cannot write to {target}: " in err and "ermission" in err, err
        assert list(locked.iterdir()) == []
    finally:
        locked.chmod(0o700)


def test_a_missing_to_is_left_alone_until_there_is_something_to_write(capsys, tmp_path):
    out_dir = tmp_path / "does" / "not" / "exist"
    code, out, err = plate(capsys, "export", "run_nope", "--to", str(out_dir))
    assert code == cli.EXIT_USAGE and "--project" in err  # the unknown run, not the folder
    assert not (tmp_path / "does").exists()


@needs_room_a
def test_a_bad_to_records_no_override(capsys, tmp_path):
    rid, env = gate_fail_run(capsys, tmp_path)
    afile = tmp_path / "afile"
    afile.write_text("hi", encoding="utf-8")
    code, out, err = plate(
        capsys,
        "export",
        rid,
        "--to",
        str(afile),
        "--override-reason",
        "a reason that must not be recorded",
        env=env,
    )
    assert code == cli.EXIT_USAGE and "--to must be a folder" in err
    assert records(tmp_path, "override", rid) == [] and run_notes(rid) == []
