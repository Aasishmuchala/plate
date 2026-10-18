"""The stress scripts (scripts/stress/*.py) read as text: they compile, they import only what they may, every one
defines CHECKS, run_all runs every one, and docs/stress.md names every one. Nothing here starts a daemon: the
scripts run under `make stress` (opt-in), never under pytest.

What a script may import (router/PLAN.md T149): the standard library, httpx and the shared `_lib`; `max_plugin`
also `plate_max` and the stubs under plugins/max/tests; `mcp_cli_imports` also `plate_daemon.mcp_server` (it runs
`plate_daemon.cli` as a subprocess, which is not an import). Everything else reaches the daemon over HTTP.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STRESS = ROOT / "scripts" / "stress"
DOC = ROOT / "docs" / "stress.md"
SCRIPTS = sorted(p for p in STRESS.glob("*.py"))
CHECK_SCRIPTS = [p for p in SCRIPTS if p.stem not in ("_lib", "run_all")]
MAX_STUBS = {p.stem for p in (ROOT / "plugins" / "max" / "tests").glob("*.py")}
EXTRA = {
    "max_plugin": lambda mod: mod == "plate_max" or mod.startswith("plate_max.") or mod in MAX_STUBS,
    "mcp_cli_imports": lambda mod: mod == "plate_daemon.mcp_server",
}
SPEC_SCRIPTS = {
    "api_fuzz",
    "doctrine",
    "money",
    "crash",
    "engine_faults",
    "scale",
    "ui",
    "max_plugin",
    "mcp_cli_imports",
}


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported(module: ast.Module) -> list[str]:
    """Every module an import statement names, anywhere in the file (`from a import b` as `a.b` and `a`)."""
    out: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names if node.module == "plate_daemon"]
    return out


def allowed(script: str, mod: str) -> bool:
    top = mod.split(".")[0]
    if top in sys.stdlib_module_names or top in ("__future__", "httpx", "_lib"):
        return True
    if script in EXTRA and EXTRA[script](mod):
        return True
    return script == "mcp_cli_imports" and mod == "plate_daemon"  # `from plate_daemon import mcp_server`


def test_the_import_rule_refuses_what_the_scripts_may_not_import():
    assert not allowed("api_fuzz", "plate_daemon") and not allowed("api_fuzz", "plate_daemon.pipeline")
    assert (
        not allowed("scale", "numpy")
        and not allowed("money", "blake3")
        and not allowed("crash", "portalocker")
    )
    assert not allowed("mcp_cli_imports", "plate_daemon.pipeline") and not allowed(
        "max_plugin", "plate_daemon"
    )
    assert allowed("max_plugin", "plate_max.ui.controller") and allowed("max_plugin", "fake_pymxs")
    assert allowed("mcp_cli_imports", "plate_daemon.mcp_server") and allowed("ui", "httpx")
    assert allowed("doctrine", "subprocess") and allowed("doctrine", "_lib")


def test_the_nine_scripts_and_the_two_helpers_are_there():
    assert {p.stem for p in CHECK_SCRIPTS} == SPEC_SCRIPTS
    assert (STRESS / "_lib.py").is_file() and (STRESS / "run_all.py").is_file()


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_every_stress_script_compiles(path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_every_stress_script_imports_only_what_it_may(path):
    bad = [m for m in imported(tree(path)) if not allowed(path.stem, m)]
    assert bad == [], f"{path.name} imports {bad}: reach the daemon over HTTP"


def test_mcp_cli_imports_takes_nothing_of_the_daemon_but_the_mcp_server():
    mods = imported(tree(STRESS / "mcp_cli_imports.py"))
    daemon = sorted({m for m in mods if m.startswith("plate_daemon")})
    assert daemon == ["plate_daemon", "plate_daemon.mcp_server"], daemon


def check_names(module: ast.Module) -> list[str]:
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CHECKS" for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict), "CHECKS is a dict of check name -> function"
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError("no module-level CHECKS")


@pytest.mark.parametrize("path", CHECK_SCRIPTS, ids=lambda p: p.stem)
def test_every_script_defines_checks_that_name_its_functions_and_runs_main(path):
    module = tree(path)
    names = check_names(module)
    assert names and len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", n) for n in names), names
    funcs = {n.name for n in module.body if isinstance(n, ast.FunctionDef)}
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CHECKS" for t in node.targets
        ):
            assert all(isinstance(v, ast.Name) and v.id in funcs for v in node.value.values)
    assert "_lib.main(CHECKS)" in path.read_text(encoding="utf-8")


def test_run_all_runs_every_script_in_the_plans_order():
    module = tree(STRESS / "run_all.py")
    found = None
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SCRIPTS" for t in node.targets
        ):
            found = [e.value for e in node.value.elts]  # type: ignore[attr-defined]
    assert found == [
        "api_fuzz",
        "doctrine",
        "money",
        "crash",
        "engine_faults",
        "scale",
        "ui",
        "max_plugin",
        "mcp_cli_imports",
    ]


def test_the_doc_names_every_script_and_every_check():
    doc = DOC.read_text(encoding="utf-8")
    for path in CHECK_SCRIPTS:
        assert f"## {path.stem}" in doc, f"docs/stress.md has no section for {path.stem}"
        assert f"{path.stem}.py" in doc
        for name in check_names(tree(path)):
            assert f"`{name}`" in doc, f"docs/stress.md does not name {path.stem}'s check {name}"


def test_the_stress_target_is_opt_in_everywhere():
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^\.PHONY:.*\bstress\b", make, re.M)
    assert "\nstress:\n\t$(PY) scripts/stress/run_all.py\n" in make
    assert "make stress    the attackers' regression scripts (opt-in; each starts its own daemon)" in make
    just = (ROOT / "justfile").read_text(encoding="utf-8")
    assert (
        "# the attackers' regression scripts (opt-in; each starts its own daemon)\n"
        "stress:\n    {{py}} scripts/stress/run_all.py\n"
    ) in just
    for text in (make, just):
        ci_line = next(ln for ln in text.splitlines() if re.match(r"^ci\s*:", ln))
        test_line = next(ln for ln in text.splitlines() if re.match(r"^test\s*:", ln))
        assert "stress" not in ci_line and "stress" not in test_line


def test_the_stress_workflow_is_manual_with_the_ci_matrix_and_setup():
    stress = yaml.safe_load((ROOT / ".github" / "workflows" / "stress.yml").read_text(encoding="utf-8"))
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as True
    assert stress.get(True, stress.get("on")) == "workflow_dispatch"
    assert stress["name"] == "stress"
    (job,) = stress["jobs"].values()
    ci_job = ci["jobs"]["check"]
    assert job["strategy"] == ci_job["strategy"] and job["runs-on"] == ci_job["runs-on"]
    assert job["timeout-minutes"] == 90
    setup = []
    for step in ci_job["steps"]:
        setup.append(step)
        if step.get("run") == "npm ci":
            break
    assert job["steps"][: len(setup)] == setup
    assert job["steps"][len(setup) :] == [
        {"name": "stress scripts", "run": "uv run --no-sync python scripts/stress/run_all.py"}
    ]
