"""plate_max runs inside Max's Python 3.11 with the standard library only, and pymxs is imported
in exactly one place. These are static checks over the package source plus the install scripts."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import plate_max
from plate_max import rt

PKG = Path(plate_max.__file__).resolve().parent
PLUGIN = PKG.parent
REPO = PLUGIN.parent.parent
SOURCES = sorted(PKG.rglob("*.py"))
FORBIDDEN = {"PySide2", "PySide6", "MaxPlus", "numpy", "PIL", "platformdirs", "blake3", "jsonschema"}


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node, node.module.split(".")[0]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_imports_are_stdlib_or_plate_max(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))
    for node, top in _imports(tree):
        if top == "pymxs":
            continue  # checked separately
        assert top in sys.stdlib_module_names or top == "plate_max", (
            f"{path.name}:{node.lineno} imports {top}"
        )
        assert top not in FORBIDDEN


def test_pymxs_is_imported_only_inside_rt_runtime():
    hits = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, top in _imports(tree):
            if top == "pymxs":
                hits.append((path.name, node.lineno))
    assert [h[0] for h in hits] == ["rt.py"], hits
    tree = ast.parse((PKG / "rt.py").read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "runtime")
    inside = {n.lineno for n in ast.walk(func) if isinstance(n, ast.Import)}
    assert hits[0][1] in inside


def test_every_module_imports_cleanly_with_the_fake_runtime():
    from fake_pymxs import FakeRuntime

    rt.set_runtime(FakeRuntime())
    for info in pkgutil.walk_packages([str(PKG)], prefix="plate_max."):
        importlib.import_module(info.name)


def test_modules_import_when_pymxs_is_blocked():
    code = (
        "import sys; sys.modules['pymxs'] = None; import pkgutil, importlib, plate_max; "
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(plate_max.__path__, 'plate_max.')]"
    )
    env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_startup_script_compiles():
    src = (PLUGIN / "startup" / "plate_startup.py").read_text(encoding="utf-8")
    compile(src, "plate_startup.py", "exec")


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1"])
def test_powershell_scripts_are_ascii_and_target_the_right_files(name):
    raw = (PLUGIN / "scripts" / name).read_bytes()
    raw.decode("ascii")  # Windows PowerShell 5.1 reads BOM-less files as ANSI
    text = raw.decode("ascii")
    assert "config.json" in text
    assert "scripts\\startup" in text
    assert "plate_startup.py" in text


def test_install_bat_wraps_install_ps1():
    text = (PLUGIN / "scripts" / "install.bat").read_bytes().decode("ascii")
    assert "-ExecutionPolicy Bypass" in text
    assert "install.ps1" in text
    assert "%~dp0" in text and "%*" in text


def test_scripts_readme_covers_the_operational_warnings():
    text = (PLUGIN / "scripts" / "README.md").read_text(encoding="utf-8")
    assert "Safe Scene Script Execution" in text
    assert "delivery week" in text
    assert "uninstall" in text.lower()
    assert REPO.joinpath("docs", "max-plugin.md").is_file()
