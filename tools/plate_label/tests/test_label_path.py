"""Where the labeller keeps its labels (router plan T110): the data folder by default, never the repo.

`<data home>/calib/labels.jsonl`, the data home being PLATE_HOME or the OS app-data folder, with PLATE_LABELS
naming the exact file and `--out` overriding both. The daemon's verdict route resolves the same file. A
repo-local `calib/labels.jsonl` left by an older build is read once with a printed warning and never written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plate_label import store
from plate_label.cli import build_parser, main
from plate_label.store import (
    append_label,
    default_labels_path,
    labelled_keys,
    read_labels,
    remove_line,
)

REPO = Path(store.__file__).resolve().parents[2]
TOOLS = REPO / "tools"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory, monkeypatch):
    """No PLATE_HOME / PLATE_LABELS, and the legacy file pointed at a folder that does not exist (a dev
    checkout may hold a real one); the once-per-process legacy cache starts empty."""
    monkeypatch.delenv("PLATE_HOME", raising=False)
    monkeypatch.delenv("PLATE_LABELS", raising=False)
    monkeypatch.setattr(store, "LEGACY_OUT", tmp_path_factory.mktemp("oldrepo") / "calib" / "labels.jsonl")
    monkeypatch.setattr(store, "_LEGACY_SEEN", {})


def line(take: str, rater: str = "aasish", plate: str | None = "in/a.png", verdict: str = "fix") -> str:
    rec = {
        "v": 1,
        "at": "2026-10-17T09:41:07Z",
        "rater": rater,
        "take_id": take,
        "plate": plate,
        "candidate": f"out/{take}.png",
        "verdict": verdict,
        "reasons": [],
        "seconds": 3.0,
    }
    return json.dumps(rec, separators=(",", ":"))


def write_legacy(lines: list[str], tail: str = "") -> Path:
    legacy = Path(store.LEGACY_OUT)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(("\n".join(lines) + "\n" + tail).encode("utf-8"))
    return legacy


# ---------------------------------------------------------------------------- the default path


def test_default_is_under_plate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PLATE_HOME", str(home))
    assert store.data_home() == home
    assert default_labels_path() == home / "calib" / "labels.jsonl"


def test_default_without_plate_home_is_the_os_data_dir(tmp_path, monkeypatch):
    calls = []

    def fake_user_data_dir(*args, **kwargs):
        calls.append((args, kwargs))
        return str(tmp_path / "appdata" / "Plate")

    monkeypatch.setattr(store.platformdirs, "user_data_dir", fake_user_data_dir)
    assert default_labels_path() == tmp_path / "appdata" / "Plate" / "calib" / "labels.jsonl"
    assert calls == [(("Plate",), {"appauthor": False})]


def test_plate_labels_names_the_exact_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLATE_LABELS", str(tmp_path / "x" / "mine.jsonl"))
    assert default_labels_path() == tmp_path / "x" / "mine.jsonl"


def test_an_explicit_environment_mapping_is_honoured(tmp_path):
    assert default_labels_path({"PLATE_HOME": str(tmp_path)}) == tmp_path / "calib" / "labels.jsonl"
    assert default_labels_path({"PLATE_LABELS": "l.jsonl", "PLATE_HOME": str(tmp_path)}) == Path("l.jsonl")


def test_default_is_never_inside_the_repo():
    assert REPO not in default_labels_path().parents  # the real OS data dir on this machine
    assert REPO not in store.data_home().parents


def test_the_default_is_resolved_when_the_tool_runs_and_the_help_names_no_repo_path(
    tmp_path, monkeypatch, capsys
):
    parser = build_parser()
    assert parser.parse_args([]).out is None  # resolved by main(), so the environment of that moment counts
    assert "<repo>" not in parser.format_help() and "$PLATE_LABELS" in parser.format_help()
    for home in ("a", "b"):
        monkeypatch.setenv("PLATE_HOME", str(tmp_path / home))
        assert main(["--stats"]) == 0
        assert str(tmp_path / home / "calib" / "labels.jsonl") in capsys.readouterr().out


def run_cli(tmp_path: Path, *argv: str, **env: str) -> subprocess.CompletedProcess:
    child = {k: v for k, v in os.environ.items() if k not in ("PLATE_HOME", "PLATE_LABELS")}
    child.update(env)
    child["PYTHONPATH"] = os.pathsep.join([str(TOOLS), child.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    child["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "plate_label", *argv],
        cwd=tmp_path,
        env=child,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_a_fresh_labeller_process_writes_to_plate_home_and_reads_it_back(tmp_path, make_image):
    img = make_image(tmp_path / "cand.png")
    home = tmp_path / "home"
    argv = ["--headless", "--rater", "r1", "--take-id", "S3", "--candidate", str(img), "--verdict", "fix"]
    done = run_cli(tmp_path, *argv, "--reasons", "P1", PLATE_HOME=str(home))
    assert done.returncode == 0, done.stderr
    labels = home / "calib" / "labels.jsonl"
    (rec,) = [json.loads(x) for x in labels.read_text(encoding="utf-8").splitlines()]
    assert rec["take_id"] == "S3" and rec["verdict"] == "fix" and rec["reasons"] == ["P1"]

    stats = run_cli(tmp_path, "--stats", PLATE_HOME=str(home))
    assert stats.returncode == 0, stats.stderr
    assert str(labels) in stats.stdout and "label(s)" in stats.stdout

    # PLATE_LABELS moves it, and --out beats both
    moved = tmp_path / "moved.jsonl"
    done = run_cli(tmp_path, *argv, PLATE_HOME=str(home), PLATE_LABELS=str(moved))
    assert done.returncode == 0, done.stderr
    assert len(moved.read_text(encoding="utf-8").splitlines()) == 1
    explicit = tmp_path / "explicit.jsonl"
    done = run_cli(tmp_path, *argv, "--out", str(explicit), PLATE_HOME=str(home), PLATE_LABELS=str(moved))
    assert done.returncode == 0, done.stderr
    assert len(explicit.read_text(encoding="utf-8").splitlines()) == 1
    assert len(labels.read_text(encoding="utf-8").splitlines()) == 1  # untouched by the other two


# ---------------------------------------------------------------------------- the legacy repo-local file


def test_a_legacy_file_is_read_once_with_a_warning_and_never_written(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    default = default_labels_path()
    legacy = write_legacy([line("old1"), line("old2", rater="other")], tail='{"v":1,"torn')
    before = legacy.read_bytes()

    first = read_labels(default)  # the data folder has no labels file yet: the legacy records stand in
    err = capsys.readouterr().err
    assert [r["take_id"] for r in first] == ["old1", "old2"]
    assert err.count("older build") == 1
    assert str(legacy) in err and str(default) in err and "never written" in err and "2 label(s)" in err

    new = line("new1")
    rec = json.loads(new)
    from plate_label.store import LabelRecord

    written = append_label(
        default,
        LabelRecord(**{k: rec[k] for k in ("at", "rater", "take_id", "plate", "candidate", "verdict")}),
    )
    assert [r["take_id"] for r in read_labels(default)] == ["old1", "old2", "new1"]
    assert read_labels(default, legacy=False) == [json.loads(written)]
    assert capsys.readouterr().err == ""  # one warning per process

    assert remove_line(default, written) is True  # undo only ever touches the data folder file
    assert [r["take_id"] for r in read_labels(default)] == ["old1", "old2"]
    assert legacy.read_bytes() == before


def test_already_labelled_keys_include_the_legacy_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    write_legacy([line("old1"), line("old2", rater="other")])
    assert labelled_keys(default_labels_path(), "aasish") == {("old1", "in/a.png", "out/old1.png")}


def test_a_path_that_is_not_the_default_does_not_see_the_legacy_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    write_legacy([line("old1")])
    other = tmp_path / "other.jsonl"
    other.write_text(line("mine") + "\n", encoding="utf-8", newline="\n")
    assert [r["take_id"] for r in read_labels(other)] == ["mine"]
    assert labelled_keys(other, "aasish") == {("mine", "in/a.png", "out/mine.png")}
    assert capsys.readouterr().err == ""


def test_no_legacy_file_means_no_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    assert read_labels(default_labels_path()) == []
    assert capsys.readouterr().err == ""


def test_the_legacy_file_is_not_legacy_when_it_is_the_target(tmp_path, monkeypatch, capsys):
    legacy = write_legacy([line("only")])
    monkeypatch.setenv("PLATE_LABELS", str(legacy))  # pointed at the repo file on purpose
    assert default_labels_path() == legacy
    assert [r["take_id"] for r in read_labels(legacy)] == ["only"]  # once, not twice
    assert capsys.readouterr().err == ""


def test_stats_counts_the_legacy_labels_of_the_default_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    write_legacy([line("old1"), line("old2")])
    assert main(["--stats"]) == 0
    captured = capsys.readouterr()
    assert "2 label(s)" in captured.out and "older build" in captured.err
