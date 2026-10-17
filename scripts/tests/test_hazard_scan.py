"""scripts/hazard_scan.py proves itself: every rule fires on a planted hazard, clean code stays clean.

scripts/ is not a Python package and is not in pytest's testpaths yet; run this file explicitly
(`.venv/bin/pytest scripts/tests -q`). CI also runs `python scripts/hazard_scan.py --selftest`.
Hazards are assembled from pieces so this file passes its own scan.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "hazard_scan.py"
spec = importlib.util.spec_from_file_location("hazard_scan_under_test", SCRIPT)
hs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hs
spec.loader.exec_module(hs)

TMP = "/" + "tmp"
CASES = [
    ("daemon/a.py", "import f" + "cntl\n", "fcntl"),
    ("daemon/a.py", "from f" + "cntl import flock\n", "fcntl"),
    ("daemon/a.py", "import os\nos." + "fork()\n", "fork"),
    ("daemon/a.py", "import os\nos." + "symlink(a, b)\n", "symlink"),
    ("daemon/a.py", "p.symlink" + "_to(q)\n", "symlink"),
    ("daemon/a.py", "subprocess.run(cmd, shell=" + "True)\n", "shell-true"),
    ("daemon/a.py", "import os\nos." + "system('dir')\n", "shell-true"),
    ("daemon/a.py", "open('x.txt')\n", "open-encoding"),
    ("daemon/a.py", "open('x.txt', 'w')\n", "open-encoding"),
    ("daemon/a.py", "p.read_text()\n", "open-encoding"),
    ("daemon/a.py", "import os\nos." + "rename(a, b)\n", "os-rename"),
    ("daemon/a.py", "from os import " + "rename\n", "os-rename"),
    ("daemon/a.py", f"p = '{TMP}/x'\n", "tmp-literal"),
    ("pixels/a.py", "p = Path(f'out/{a}:{b}.json')\n", "colon-filename"),
    ("gates/a.py", "name = '%s:%s.png' % (a, b)\n", "colon-filename"),
    ("gates/a.py", "q = Path(when.isoformat())\n", "colon-filename"),
    ("daemon/a.py", "cmd = ['bash', 'run." + "sh']\n", "sh-reference"),
    ("daemon/a.py", "p = 'C:\\\\Users\\\\a'\n", "backslash-path"),
    ("daemon/a.py", "p = '\\\\\\\\nas\\\\share'\n", "backslash-path"),
    ("daemon/a.py", "p = 'plugins\\\\max'\n", "backslash-path"),
    ("daemon/a.py", "p = '" + "dir/" * 60 + "'\n", "max-path"),
]
CLEAN = [
    ("daemon/a.py", "import os\nos.replace(a, b)\n"),
    (
        "daemon/a.py",
        "open('x.bin', 'rb')\nopen('x.txt', 'w', encoding='utf-8')\np.read_text(encoding='utf-8')\n",
    ),
    ("daemon/a.py", "raise ValueError(f'{path}: not found')\nlog.info(f'{a}:{b}')\n"),
    ("daemon/a.py", "p = Path(stamp.strftime('%Y%m%dT%H%M%SZ'))\n"),
    ("daemon/a.py", f'"""Docstring naming {TMP}/x and os.' + "rename and build." + 'sh."""\n'),
    ("daemon/tests/test_a.py", "p = 'D:\\\\PLATE\\\\packets'\n"),  # Windows-path fixtures are fine in tests
    ("engines/a.py", "p = Path(f'{a}:{b}.json')\n"),  # the colon rule covers daemon/ pixels/ gates/ only
    ("daemon/a.py", "webbrowser.open(url)\nImage.open(p)\nsubprocess.run(['a'], shell=False)\n"),
    ("daemon/a.py", "x = '# not a path'\ny = 'a\\nb'\nz = r'(\\w+)\\.json'\n"),
]


@pytest.mark.parametrize("rel, src, rule", CASES, ids=[f"{c[2]}-{i}" for i, c in enumerate(CASES)])
def test_rule_fires(rel, src, rule):
    assert rule in {f.rule for f in hs.scan_python(rel, src)}


@pytest.mark.parametrize("rel, src", CLEAN, ids=[f"clean-{i}" for i in range(len(CLEAN))])
def test_clean_code_passes(rel, src):
    assert hs.scan_python(rel, src) == []


def test_syntax_error_is_reported_not_skipped():
    assert {f.rule for f in hs.scan_python("daemon/a.py", "def (:\n")} == {"parse"}


def test_selftest_passes(capsys):
    assert hs.selftest() == 0
    assert "selftest ok" in capsys.readouterr().out


def test_allowlist_matches_path_line_and_rule(tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text(
        "# comment\ndaemon/*.py:3 tmp-literal  # why\ntests/test_x.py *\n", encoding="utf-8", newline="\n"
    )
    entries = hs.load_allow(allow)
    hit = hs.Finding("daemon/a.py", 3, "tmp-literal", "m")
    assert hs.apply_allow([hit], entries) == []
    other_line = hs.Finding("daemon/a.py", 4, "tmp-literal", "m")
    other_rule = hs.Finding("daemon/a.py", 3, "fork", "m")
    wildcard = hs.Finding("tests/test_x.py", 9, "fork", "m")
    assert hs.apply_allow([other_line, other_rule], entries) == [other_line, other_rule]
    assert hs.apply_allow([wildcard], entries) == []


def test_allowlist_rejects_unknown_rules(tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text("daemon/a.py no-such-rule\n", encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit):
        hs.load_allow(allow)


def test_ps1_must_be_crlf(tmp_path):
    (tmp_path / ".gitattributes").write_text("*.ps1 text eol=crlf\n", encoding="utf-8", newline="\n")
    good = tmp_path / "good.ps1"
    good.write_bytes(b"Write-Host 'a'\r\n")
    bad = tmp_path / "bad.ps1"
    bad.write_bytes(b"Write-Host 'a'\r\nWrite-Host 'b'\n")
    found = hs.scan_repo(tmp_path, [good, bad, tmp_path / ".gitattributes"])
    assert [(f.path, f.rule) for f in found] == [("bad.ps1", "crlf-ps1")]


def test_missing_gitattributes_is_a_finding(tmp_path):
    assert [f.rule for f in hs.scan_repo(tmp_path, [])] == ["gitattributes"]


def test_the_repo_scanner_runs_end_to_end(tmp_path):
    (tmp_path / ".gitattributes").write_text("*.ps1 text eol=crlf\n", encoding="utf-8", newline="\n")
    src = tmp_path / "daemon" / "m.py"
    src.parent.mkdir()
    src.write_text("import os\nos." + "rename('a', 'b')\n", encoding="utf-8", newline="\n")
    assert hs.main(["--root", str(tmp_path), "--allow", str(tmp_path / "none.txt")]) == 1
    assert hs.main(["--list-rules"]) == 0
