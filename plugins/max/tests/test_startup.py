"""The Max startup script: loads plate_max from the configured repo, and never raises into Max."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import plate_max

PLUGIN = Path(plate_max.__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent
STARTUP = PLUGIN / "startup" / "plate_startup.py"


def test_startup_loads_plate_max_from_repo_path(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"repo_path": str(REPO)}), encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_CONFIG", str(cfg))
    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(STARTUP))
    assert f"plate_max {plate_max.PLUGIN_VERSION} loaded" in capsys.readouterr().out


def test_startup_swallows_a_missing_or_broken_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "nope.json"))
    runpy.run_path(str(STARTUP))
    assert "plugin not loaded" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setenv("PLATE_CONFIG", str(bad))
    runpy.run_path(str(STARTUP))
    assert "plugin not loaded" in capsys.readouterr().out


def test_startup_reports_a_config_without_repo_path(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"token": "t"}), encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_CONFIG", str(cfg))
    runpy.run_path(str(STARTUP))
    assert "repo_path missing" in capsys.readouterr().out
