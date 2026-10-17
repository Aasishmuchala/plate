"""The Max startup script: loads plate_max from the configured repo, and never raises into Max."""

from __future__ import annotations

import ast
import importlib
import json
import re
import runpy
import sys
import types
from pathlib import Path

import plate_max
from plate_max import bootstrap, rt

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


# ---- the PLATE_Dock macro (T20): registered by the startup script, opens the dock through bootstrap.launch()
class RecordingMacros:
    """Stands in for pymxs.runtime.macros; fake_pymxs.FakeRuntime has no macro manager."""

    def __init__(self):
        self.created = []

    def new(self, *args):
        self.created.append(args)


def _install_runtime(macros=None):
    runtime = types.SimpleNamespace(macros=macros if macros is not None else RecordingMacros())
    rt.set_runtime(runtime)
    return runtime


def test_register_macro_creates_plate_dock_in_the_plate_category():
    runtime = _install_runtime()
    assert bootstrap.register_macro() == "PLATE_Dock"
    ((category, name, tooltip, button, script),) = runtime.macros.created
    assert (category, name) == ("PLATE", "PLATE_Dock")
    assert tooltip and button
    assert "plate_max.bootstrap" in script and "launch()" in script
    assert script.startswith("python.Execute")  # Max 2026: python.Execute runs the string as Python


def test_the_macro_script_names_a_real_launcher():
    module = re.search(r"import ([\w.]+) as (\w+);\s*\2\.(\w+)\(\)", bootstrap.MACRO_SCRIPT)
    assert module, bootstrap.MACRO_SCRIPT
    target = importlib.import_module(module.group(1))
    assert callable(getattr(target, module.group(3)))


def test_startup_registers_the_dock_macro(tmp_path, monkeypatch, capsys):
    runtime = _install_runtime()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"repo_path": str(REPO)}), encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_CONFIG", str(cfg))
    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(STARTUP))
    out = capsys.readouterr().out
    assert "macro PLATE_Dock registered" in out
    assert [args[1] for args in runtime.macros.created] == ["PLATE_Dock"]


def test_startup_survives_a_runtime_that_cannot_register_the_macro(tmp_path, monkeypatch, capsys):
    rt.set_runtime(types.SimpleNamespace())  # no `macros` attribute
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"repo_path": str(REPO)}), encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_CONFIG", str(cfg))
    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(STARTUP))  # must not raise into Max
    out = capsys.readouterr().out
    assert f"plate_max {plate_max.PLUGIN_VERSION} loaded" in out
    assert "dock macro not registered" in out and "AttributeError" in out


def test_startup_has_no_auto_update_and_no_network():
    """BUILD_PLAN decision 25: updating the plugin is a git pull by hand on a quiet day."""
    tree = ast.parse(STARTUP.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"subprocess", "urllib", "http", "socket", "requests", "ssl", "ftplib"}, imported
    strings = " ".join(
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert "git pull" not in strings.replace("no git pull", "")


def test_startup_reads_the_single_app_data_config_only():
    text = STARTUP.read_text(encoding="utf-8")
    assert "plugin_" + "config" not in text  # no second config file; one config.json for everyone
    assert "PLATE_CONFIG" in text and "PLATE_HOME" in text and "config.json" in text
