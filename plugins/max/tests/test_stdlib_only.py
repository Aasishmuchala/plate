"""plate_max runs inside Max's Python 3.11 with the standard library only, and pymxs is imported
in exactly one place. Qt (qtmax, PySide6) is the one exception and is allowed only inside
plate_max/ui/, the dock, which Max hosts and nothing else imports. These are static checks over the
package source (function-local imports included) plus the install scripts."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import plate_max
from plate_max import rt

PKG = Path(plate_max.__file__).resolve().parent
PLUGIN = PKG.parent
REPO = PLUGIN.parent.parent
SOURCES = sorted(PKG.rglob("*.py"))
UI_DIR = PKG / "ui"
QT_MODULES = {"qtmax", "PySide6", "PySide2", "shiboken6"}
FORBIDDEN = {"MaxPlus", "numpy", "PIL", "platformdirs", "blake3", "jsonschema", *QT_MODULES}


def _imports(tree):
    """Every import in the module, function-local and conditional ones included (ast.walk goes
    into function bodies, try blocks and if blocks)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node, node.module.split(".")[0]


def _violations(source: str, in_ui: bool = False) -> list[str]:
    """The imports a module of plate_max may not make: anything outside the standard library and
    plate_max, except pymxs (rt.py only, checked below) and Qt inside plate_max/ui/."""
    tree = ast.parse(source, feature_version=(3, 11))
    bad = []
    for node, top in _imports(tree):
        if top == "pymxs":
            continue
        if in_ui and top in QT_MODULES:
            continue
        if top not in sys.stdlib_module_names and top != "plate_max":
            bad.append(f"line {node.lineno}: {top}")
    return bad


def _module_names():
    return [m.name for m in pkgutil.walk_packages([str(PKG)], prefix="plate_max.")]


def _headless_modules():
    """Modules that must import with no Max and no Qt (the dock under plate_max.ui needs both)."""
    return [n for n in _module_names() if not n.startswith("plate_max.ui")]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(PKG).as_posix())
def test_imports_are_stdlib_or_plate_max(path):
    in_ui = UI_DIR in path.parents
    assert _violations(path.read_text(encoding="utf-8"), in_ui) == [], path.name


def test_the_checker_sees_function_local_and_conditional_imports():
    source = textwrap.dedent(
        """
        import json

        def lazy():
            import numpy
            from PIL import Image

        try:
            import blake3
        except ImportError:
            blake3 = None

        if True:
            from plate_max import rt
        """
    )
    assert _violations(source) == ["line 5: numpy", "line 6: PIL", "line 9: blake3"]


def test_qt_is_allowed_only_inside_the_ui_package():
    source = "import qtmax\nfrom PySide6 import QtWidgets\n\ndef f():\n    from PySide6.QtCore import Qt\n"
    assert _violations(source, in_ui=True) == []
    assert _violations(source, in_ui=False) == ["line 1: qtmax", "line 2: PySide6", "line 5: PySide6"]


def test_the_real_sources_keep_qt_out_of_everything_but_ui():
    for path in SOURCES:
        if UI_DIR in path.parents:
            continue
        tops = {top for _, top in _imports(ast.parse(path.read_text(encoding="utf-8")))}
        assert not tops & QT_MODULES, f"{path.name} imports Qt outside plate_max/ui"


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
    for name in _headless_modules():
        importlib.import_module(name)


def test_modules_import_when_pymxs_is_blocked():
    code = (
        "import sys; sys.modules['pymxs'] = None; import pkgutil, importlib, plate_max; "
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(plate_max.__path__, 'plate_max.')"
        " if not m.name.startswith('plate_max.ui')]"
    )
    env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_startup_script_compiles():
    src = (PLUGIN / "startup" / "plate_startup.py").read_text(encoding="utf-8")
    compile(src, "plate_startup.py", "exec")


def test_the_package_parses_as_python_3_11():
    for path in SOURCES + [PLUGIN / "startup" / "plate_startup.py"]:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_the_package_compiles_with_a_real_python_3_11(tmp_path):
    """Max 2026 embeds 3.11: when a python3.11 is on this machine, py_compile every source with it
    (bytecode goes to tmp_path, not into the repo). Without one this assertion is skipped and the
    3.11 grammar check above is what remains."""
    python311 = shutil.which("python3.11")
    if python311 is None:
        pytest.skip("no python3.11 on this machine")
    files = [str(p) for p in SOURCES + [PLUGIN / "startup" / "plate_startup.py"]]
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")}
    done = subprocess.run(
        [python311, "-m", "py_compile", *files], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr


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
