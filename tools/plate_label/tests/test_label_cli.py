from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from plate_label import cli
from plate_label.cli import main, print_stats, split_codes
from plate_label.store import read_labels

TOOLS_DIR = Path(__file__).resolve().parents[2]


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--pairs" in text and "--rater" in text and "Ship" in text


def test_python_dash_m_help_exits_zero():
    env = {**os.environ, "PYTHONPATH": str(TOOLS_DIR)}
    proc = subprocess.run(
        [sys.executable, "-m", "plate_label", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--pairs" in proc.stdout


def test_split_codes():
    assert split_codes("a2, p4  f11") == ["A2", "P4", "F11"]
    assert split_codes("") == []


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "serve", lambda *a, **k: calls.append((a, k)) or 0)
    return calls


def test_folder_and_serve_alias_start_the_server(shots, tmp_path, captured):
    out = tmp_path / "l.jsonl"
    assert main([str(shots), "--out", str(out), "--rater", "r1", "--port", "0", "--no-open"]) == 0
    assert main(["serve", str(shots), "--out", str(out), "--rater", "r1"]) == 0
    (args1, kw1), (args2, _) = captured
    pairs, got_out, rater, port = args1
    assert (
        len(pairs) == 3 and got_out == out and rater == "r1" and port == 0 and kw1 == {"open_browser": False}
    )
    assert len(args2[0]) == 3 and args2[3] == 4612


def test_pairs_manifest_starts_the_server(manifest, tmp_path, captured):
    assert main(["--pairs", str(manifest), "--out", str(tmp_path / "l.jsonl")]) == 0
    assert [p.take_id for p in captured[0][0][0]] == ["S3", "S4"]


def test_bad_invocations_exit_2(shots, manifest, tmp_path, captured, capsys):
    assert main([]) == 2
    assert main([str(shots), "--pairs", str(manifest)]) == 2
    assert main([str(tmp_path / "missing-folder")]) == 2
    assert main(["--pairs", str(tmp_path / "none.json")]) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty)]) == 2
    err = capsys.readouterr().err
    assert "FOLDER" in err and "not both" in err and "not a folder" in err and "no images" in err
    assert captured == []  # the server never started


def test_headless_writes_one_record(tmp_path, make_image, capsys):
    img = make_image(tmp_path / "cand.png")
    plate = make_image(tmp_path / "plate.png")
    out = tmp_path / "labels.jsonl"
    argv = ["--headless", "--out", str(out), "--rater", "r1", "--take-id", "S3", "--candidate", str(img),
            "--plate", str(plate), "--verdict", "fix", "--reasons", "p1, a2", "--seconds", "4.5",
            "--run-id", "r9", "--gate-report", "gates/r9.1.json"]  # fmt: skip
    assert main(argv) == 0
    (rec,) = read_labels(out)
    assert rec["reasons"] == ["P1", "A2"] and rec["seconds"] == 4.5 and rec["run_id"] == "r9"
    assert rec["gate_report"] == "gates/r9.1.json" and rec["take_id"] == "S3" and rec["rater"] == "r1"
    assert "labelled S3" in capsys.readouterr().out


def test_headless_errors_write_nothing(tmp_path, make_image, capsys):
    img = make_image(tmp_path / "cand.png")
    out = tmp_path / "labels.jsonl"
    base = ["--headless", "--out", str(out), "--take-id", "S3", "--candidate", str(img)]
    assert main(base) == 2  # no verdict
    assert main(["--headless", "--out", str(out), "--verdict", "ship"]) == 2  # no take / candidate
    assert main([*base, "--verdict", "fix", "--reasons", "Z9"]) == 2
    assert main([*base, "--verdict", "ship", "--reasons", "P1"]) == 2
    assert main([*base[:-1], str(tmp_path / "nope.png"), "--verdict", "ship"]) == 2
    err = capsys.readouterr().err
    assert "--headless needs" in err and "Z9" in err and "does not exist" in err
    assert not out.exists()


def test_stats(tmp_path, make_image, capsys):
    img = make_image(tmp_path / "cand.png")
    out = tmp_path / "labels.jsonl"
    for verdict, reasons in [("ship", ""), ("fix", "P1,A2"), ("fix", "P1"), ("render_it", "F6")]:
        argv = ["--headless", "--out", str(out), "--rater", "r1", "--take-id", "S3", "--candidate", str(img),
                "--verdict", verdict, "--reasons", reasons, "--seconds", "3"]  # fmt: skip
        assert main(argv) == 0
    capsys.readouterr()
    assert main(["stats", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "4 label(s)" in text and "r1  4" in text
    lines = {
        ln.split()[0]: ln.split()[1]
        for ln in text.splitlines()
        if ln.strip() and ln.split()[0] in {"ship", "fix", "render_it", "P1", "A2", "F6", "A1"}
    }
    assert lines == {"ship": "1", "fix": "2", "render_it": "1", "P1": "2", "A2": "1", "F6": "1", "A1": "0"}
    assert "median time per label" in text
    assert print_stats(tmp_path / "none.jsonl", print_fn=lambda *a: None) == 0
