from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from blake3 import blake3

from plate_label.cli import main, print_stats, run_interactive, split_codes
from plate_label.store import read_labels

REPO = Path(__file__).resolve().parents[3]


def run_cli(*args: str, out: Path | None = None):
    env = {**os.environ, "PYTHONPATH": str(REPO / "tools")}
    cmd = [sys.executable, "-m", "plate_label", *args]
    if out is not None:
        cmd += ["--out", str(out)]
    return subprocess.run(
        cmd, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
    )


def test_help_lists_headless():
    r = run_cli("--help")
    assert r.returncode == 0 and "--headless" in r.stdout


def test_headless_writes_one_record_with_matching_hash(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    img = images / "A_first.jpg"
    r = run_cli(
        "--headless", str(img), "--verdict", "fix", "--reasons", "g01,C01",
        "--note", "edge shift", "--rater", "tester", "--take-id", "t1", out=out,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["image_blake3"] == blake3(img.read_bytes()).hexdigest()
    assert rec["image"] == "A_first.jpg" and rec["verdict"] == "fix"
    assert rec["reasons"] == ["G01", "C01"] and rec["rater"] == "tester" and rec["take_id"] == "t1"


def test_headless_folder_relative_image_and_subdir(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    r = run_cli("--headless", "sub/c_third.webp", str(images), "--verdict", "ship", out=out)
    assert r.returncode == 0, r.stderr
    assert read_labels(out)[0]["image"] == "sub/c_third.webp"


def test_headless_validation_errors_exit_2(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    img = str(images / "A_first.jpg")
    for extra, needle in [
        (["--verdict", "fix"], "at least one"),
        (["--verdict", "fix", "--reasons", "Z99"], "Z99"),
        (["--verdict", "fix", "--reasons", "O99"], "O99"),
        ([], "--verdict"),
    ]:
        r = run_cli("--headless", img, *extra, out=out)
        assert r.returncode == 2 and needle in r.stderr, (extra, r.stderr)
    assert run_cli("--headless", str(images / "nope.png"), "--verdict", "ship", out=out).returncode == 2
    assert not out.exists()


def test_serve_alias_needs_a_folder(tmp_path):
    r = run_cli("serve", out=tmp_path / "labels.jsonl")
    assert r.returncode == 2 and "FOLDER" in r.stderr


def test_stats(images, tmp_path, capsys):
    out = tmp_path / "labels.jsonl"
    run_cli("--headless", str(images / "A_first.jpg"), "--verdict", "ship", out=out)
    run_cli("--headless", str(images / "b_second.PNG"), "--verdict", "fix", "--reasons", "G01,T02", out=out)
    run_cli(
        "--headless", str(images / "sub" / "c_third.webp"), "--verdict", "fix", "--reasons", "G01", out=out
    )
    assert main(["stats", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "3 label(s)" in text
    assert any(line.split()[:2] == ["fix", "2"] for line in text.splitlines())
    assert any(line.split()[:2] == ["G01", "2"] for line in text.splitlines())
    assert any(line.split()[:2] == ["T02", "1"] for line in text.splitlines())
    assert print_stats(tmp_path / "missing.jsonl", print_fn=lambda *a, **k: None) == 0


def test_split_codes():
    assert split_codes("g01, C01  x01") == ["G01", "C01", "X01"]
    assert split_codes("") == []


def scripted(lines):
    it = iter(lines)

    def input_fn(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return input_fn


def test_interactive_loop(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    shown: list[str] = []
    say = lambda *a, **k: shown.append(" ".join(str(x) for x in a))  # noqa: E731
    # image 1: ship; image 2: fix without a reason (rejected), then fix with reasons plus a note; image 3: quit
    code = run_interactive(
        images, out, "tester", "take-1",
        input_fn=scripted(["s", "f", "n", "ledge is soft", "f G01,t02", "q"]), print_fn=say,
    )  # fmt: skip
    assert code == 0
    rows = read_labels(out)
    assert [(r["image"], r["verdict"]) for r in rows] == [("A_first.jpg", "ship"), ("b_second.PNG", "fix")]
    assert rows[1]["reasons"] == ["G01", "T02"] and rows[1]["note"] == "ledge is soft"
    assert rows[1]["rater"] == "tester" and rows[1]["take_id"] == "take-1"
    assert any(s.startswith("1/3 A_first.jpg") for s in shown) and any(
        s.startswith("3/3 sub/c_third.webp") for s in shown
    )
    assert any("at least one" in s for s in shown)

    # a second session resumes at the unlabelled image only
    shown.clear()
    run_interactive(images, out, "tester", None, input_fn=scripted(["r G03"]), print_fn=say)
    assert [r["verdict"] for r in read_labels(out)] == ["ship", "fix", "render_it"]
    assert any("2 already labelled" in s for s in shown) and not any(s.startswith("1/3") for s in shown)

    shown.clear()
    assert run_interactive(images, out, "tester", None, input_fn=scripted([]), print_fn=say) == 0
    assert any("nothing left" in s for s in shown)
