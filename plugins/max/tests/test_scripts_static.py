"""Scripts that only ever EXECUTE inside Max or on a Windows box must still be VERIFIABLE here.

Same semantics as MaxDirector's tests/test_scripts_static.py (its round-3 lesson: a refactor removed a
symbol, an ImportError guard swallowed the failure, and the box printed a lie instead of the error).
Every Python script under plugins/max/scripts and plugins/max/startup must compile as Python 3.11,
every `from plate_max... import X` must name a real X, and a script's own entry calls must never sit
inside an ImportError guard. The PowerShell installers cannot run on this Mac, so what is checked
statically is what a typo would break: the parameters, strict mode, the files they name, that
uninstall touches only what install put there, and that neither script installs, downloads or updates
anything (BUILD_PLAN decision 25). Modules that need Qt or pymxs are verified from their AST when the
import itself cannot run here.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

import plate_max

PLUGIN = Path(plate_max.__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent
SCRIPT_DIRS = [PLUGIN / "scripts", PLUGIN / "startup"]
SCRIPTS = sorted(p for d in SCRIPT_DIRS for p in d.glob("*.py"))
SCRIPT_IDS = [p.relative_to(PLUGIN).as_posix() for p in SCRIPTS]
FIRST_PARTY = ("plate_max",)
# Modules that exist only inside 3ds Max (or its shipped Qt): an import that dies on one of these here
# falls back to AST verification; anything else must really import.
HOST_ONLY = {"pymxs", "MaxPlus", "qtmax", "PySide2", "PySide6", "shiboken2", "shiboken6"}
# Scripts that must import first-party code (False = a pure standard-library script, by design).
MUST_IMPORT_PLATE_MAX = {"plate_startup.py": True, "ui_preview.py": True}


def _source_of(dotted: str) -> Path | None:
    base = PLUGIN.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _module_level_names(path: Path) -> set[str]:
    """Names bound at module level (recursing into if/try/with/loop bodies): what `from M import X`
    can see once M executes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    def visit(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(s.name)
            elif isinstance(s, ast.Assign):
                for t in s.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        names.update(e.id for e in t.elts if isinstance(e, ast.Name))
            elif isinstance(s, (ast.AnnAssign, ast.AugAssign)) and isinstance(s.target, ast.Name):
                names.add(s.target.id)
            elif isinstance(s, (ast.Import, ast.ImportFrom)):
                for a in s.names:
                    names.add("*" if a.name == "*" else (a.asname or a.name).split(".")[0])
            elif isinstance(s, ast.If):
                visit(s.body)
                visit(s.orelse)
            elif isinstance(s, ast.Try):
                visit(s.body)
                visit(s.orelse)
                visit(s.finalbody)
                for h in s.handlers:
                    visit(h.body)
            elif isinstance(s, ast.With):
                visit(s.body)
            elif isinstance(s, (ast.For, ast.While)):
                visit(s.body)
                visit(s.orelse)

    visit(tree.body)
    return names


def _first_party_import_ok(module: str, name: str) -> bool:
    """True iff `from <module> import <name>` would succeed on the box."""
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        if (exc.name or "").split(".")[0] not in HOST_ONLY:
            raise  # a first-party module is itself broken: surface it loudly
        mod = None
    if mod is not None:
        if hasattr(mod, name):
            return True
        try:  # `from pkg import submodule` is legal even when pkg does not re-export it
            return importlib.util.find_spec(f"{module}.{name}") is not None
        except (ImportError, ValueError):
            return False
    src = _source_of(module)
    assert src is not None, f"{module} has no source under plugins/max"
    names = _module_level_names(src)
    return "*" in names or name in names or _source_of(f"{module}.{name}") is not None


def test_the_scripts_to_check_were_found():
    assert {p.name for p in SCRIPTS} >= {"plate_startup.py", "ui_preview.py"}, SCRIPT_IDS


@pytest.mark.parametrize("path", SCRIPTS, ids=SCRIPT_IDS)
def test_script_compiles_and_first_party_imports_resolve(path):
    src = path.read_text(encoding="utf-8")
    compile(src, path.name, "exec")  # syntax floor
    ast.parse(src, filename=path.name, feature_version=(3, 11))  # Max 2026 embeds 3.11
    checked = 0
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module.split(".")[0] not in FIRST_PARTY:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                assert _first_party_import_ok(node.module, alias.name), (
                    f"{path.name}: `from {node.module} import {alias.name}`: '{alias.name}' does not "
                    "exist (stale after a refactor?)"
                )
                checked += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FIRST_PARTY:
                    assert _source_of(alias.name) is not None, (
                        f"{path.name}: `import {alias.name}` no longer exists"
                    )
                    checked += 1
    if MUST_IMPORT_PLATE_MAX.get(path.name):
        assert checked > 0, f"{path.name}: no plate_max imports found: did the script move?"


@pytest.mark.parametrize("path", SCRIPTS, ids=SCRIPT_IDS)
def test_entry_work_never_sits_inside_an_import_guard(path):
    """A guard may wrap `import pymxs` (or PySide6) alone, never the script's own entry calls: a stale
    import would masquerade as 'not inside 3ds Max' (the MaxGaffer round-3 bug)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entry_names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handled: set[str] = set()
        for h in node.handlers:
            if isinstance(h.type, ast.Name):
                handled.add(h.type.id)
            elif isinstance(h.type, ast.Tuple):
                handled.update(e.id for e in h.type.elts if isinstance(e, ast.Name))
        if not handled & {"ImportError", "ModuleNotFoundError"}:
            continue
        body_calls = {
            c.func.id
            for stmt in node.body
            for c in ast.walk(stmt)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        bad = sorted(body_calls & entry_names)
        assert not bad, f"{path.name}: {bad} called inside an ImportError guard"


def test_the_startup_script_never_swallows_a_failure_silently():
    """It must never raise into Max, but every handler has to say what went wrong in the Listener."""
    tree = ast.parse((PLUGIN / "startup" / "plate_startup.py").read_text(encoding="utf-8"))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers
    for handler in handlers:
        prints = [
            c for c in ast.walk(handler) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "print"
        ]
        assert prints, f"line {handler.lineno}: an except block that does not print"


def test_the_startup_script_registers_the_macro_through_bootstrap():
    src = (PLUGIN / "startup" / "plate_startup.py").read_text(encoding="utf-8")
    assert "bootstrap" in src and "register_macro" in src
    assert _first_party_import_ok("plate_max.bootstrap", "register_macro")
    assert _first_party_import_ok("plate_max.bootstrap", "launch")


# ------------------------------------------------------------------ the PowerShell installers
def _code_lines(path: Path) -> list[str]:
    """The script's executable lines: comment-based help blocks and # comments removed."""
    text = path.read_text(encoding="ascii")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1"])
def test_installers_are_strict_and_stop_on_error(name):
    lines = _code_lines(PLUGIN / "scripts" / name)
    assert "Set-StrictMode -Version Latest" in lines
    assert "$ErrorActionPreference = 'Stop'" in lines
    assert any(ln.startswith("param(") for ln in lines)


def test_install_has_the_documented_parameters():
    text = (PLUGIN / "scripts" / "install.ps1").read_text(encoding="ascii")
    assert "[string]$RepoPath" in text
    assert re.search(r"\[string\]\$MaxVersion\s*=\s*'2026'", text)
    for extra in ("$Token", "$DaemonUrl", "$PacketsDir", "$StartupDir", "$ConfigPath"):
        assert extra in text


def test_install_checks_max_python_and_prints_the_safe_scene_script_note():
    text = (PLUGIN / "scripts" / "install.ps1").read_text(encoding="ascii")
    assert "Autodesk" in text and "3ds Max $MaxVersion" in text and "Python\\python.exe" in text
    assert "Safe Scene Script Execution" in text
    assert "Customize > Preferences > Scripting" in text
    assert "PLATE_Dock" in text


def test_install_copies_the_startup_script_that_exists_and_keeps_a_backup():
    text = (PLUGIN / "scripts" / "install.ps1").read_text(encoding="ascii")
    assert (PLUGIN / "startup" / "plate_startup.py").is_file()
    assert "'startup'" in text and "'plate_startup.py'" in text
    assert "scripts\\startup" in text
    assert ".bak" in text  # an existing, different startup script is kept
    assert "token" in text and "repo_path" in text and "packets_dir" in text and "daemon_url" in text


def test_install_never_overwrites_an_existing_token_unless_asked():
    lines = _code_lines(PLUGIN / "scripts" / "install.ps1")
    body = "\n".join(lines)
    # the token is only generated when none is given and none is in config.json
    assert re.search(r"if \(-not \$Token\)", body)
    assert re.search(r"\$cfg\.Contains\('token'\)", body)


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1"])
def test_installers_install_download_and_update_nothing(name):
    body = "\n".join(_code_lines(PLUGIN / "scripts" / name))
    banned = (
        r"Invoke-WebRequest|Invoke-RestMethod|Start-BitsTransfer|\bcurl\b|\bwget\b|"
        r"\bgit\s+(pull|fetch|clone|checkout)\b|\bpip3?\s+install\b|\bnpm\s+(install|ci)\b|"
        r"Install-Module|Install-Package|\bwinget\b|\bchoco\b|Set-ExecutionPolicy"
    )
    assert not re.search(banned, body, flags=re.I), re.search(banned, body, flags=re.I).group(0)


def test_uninstall_removes_only_what_install_put_there_and_leaves_backups():
    lines = _code_lines(PLUGIN / "scripts" / "uninstall.ps1")
    removals = [ln for ln in lines if ln.startswith("Remove-Item") or "| Remove-Item" in ln]
    assert removals, "uninstall.ps1 removes nothing"
    assert not any("-Recurse" in ln for ln in lines)
    assert not any(".bak" in ln for ln in removals), "backups must be left in place"
    assert all("-LiteralPath" in ln for ln in removals), "removal by an exact path only: no wildcards"
    text = "\n".join(lines)
    assert "$target = Join-Path $StartupDir 'plate_startup.py'" in text
    assert "plate_max_installed_keys" in text  # config.json keys: only the ones the installer recorded


def test_install_and_uninstall_agree_on_the_installed_keys_record():
    install = (PLUGIN / "scripts" / "install.ps1").read_text(encoding="ascii")
    uninstall = (PLUGIN / "scripts" / "uninstall.ps1").read_text(encoding="ascii")
    assert "plate_max_installed_keys" in install and "plate_max_installed_keys" in uninstall


def test_install_validates_and_absolutises_repo_path():
    body = "\n".join(_code_lines(PLUGIN / "scripts" / "install.ps1"))
    assert "plate_max\\__init__.py" in body, "RepoPath must be checked for the plugin before it is written"
    assert "Resolve-Path -LiteralPath $RepoPath" in body, "Max's working directory is not the shell's"
    assert (PLUGIN / "plate_max" / "__init__.py").is_file()


def test_install_bat_is_only_a_launcher_for_install_ps1():
    lines = [
        ln.strip() for ln in (PLUGIN / "scripts" / "install.bat").read_text(encoding="ascii").splitlines()
    ]
    code = [ln for ln in lines if ln and not ln.lower().startswith("rem ") and not ln.startswith("@echo off")]
    assert any("-File" in ln and "install.ps1" in ln and "%*" in ln for ln in code)
    assert (PLUGIN / "scripts" / "install.ps1").is_file()


# ------------------------------------------------------------------ one config.json, docs that name real files
def test_no_second_config_file_exists_anywhere_in_the_plugin():
    needle = "plugin_" + "config"  # spelled in two pieces so this file does not trip its own check
    hits = []
    for path in PLUGIN.rglob("*"):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix
            in {
                ".py",
                ".ps1",
                ".bat",
                ".md",
                ".json",
                ".txt",
                ".ms",
            }
        ):
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(path.relative_to(PLUGIN).as_posix())
    assert hits == [], hits


def _referenced_repo_paths(markdown: Path) -> list[str]:
    text = markdown.read_text(encoding="utf-8")
    found = re.findall(
        r"`((?:plugins[\\/]max[\\/]|scripts[\\/]|docs[\\/])[\w\\/.\-]+\.(?:ps1|bat|py|md|sh|png))`", text
    )
    return sorted({f.replace("\\", "/") for f in found})


@pytest.mark.parametrize(
    "readme", [PLUGIN / "README.md", PLUGIN / "scripts" / "README.md"], ids=["max", "scripts"]
)
def test_readme_paths_exist(readme):
    assert readme.is_file(), readme
    missing = [p for p in _referenced_repo_paths(readme) if not (REPO / p).exists()]
    assert missing == [], missing


def test_the_committed_dock_preview_is_a_real_png():
    preview = PLUGIN / "dock_preview.png"
    if not preview.is_file():
        pytest.skip("run plugins/max/scripts/ui_preview.py to create it")
    raw = preview.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(raw[16:20], "big") >= 400 and int.from_bytes(raw[20:24], "big") >= 600


def test_this_suite_runs_with_the_plugin_on_sys_path():
    assert str(PLUGIN) in {str(Path(p).resolve()) for p in sys.path if p}
