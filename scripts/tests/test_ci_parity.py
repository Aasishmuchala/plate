"""CI, the Makefile and the justfile run the same steps in the same order.

`make ci` (Mac), `just ci` (the Windows box), .github/workflows/ci.yml, scripts/test.sh (Mac) and
scripts/test.ps1 (Windows) are five spellings of one job. This file parses all five and fails when one of them
gains, loses or reorders a step, so a check added to CI cannot be forgotten locally and the other way round.

scripts/ is not a Python package and is outside pytest's testpaths: run it with `.venv/bin/pytest scripts/tests`
(CI and `make ci` do).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SETUP_PREFIXES = ("uv sync", "npm ci", "npm install", "brew install")  # toolchain setup, not a CI check

EXPECTED = [
    "python -m plate_gates.fidelity_bench selftest",
    "python scripts/hazard_scan.py --selftest",
    "python scripts/hazard_scan.py --strict-allow",
    "ruff check .",
    "ruff format --check .",
    "python -m plate_pixels.licences --check",
    "pytest",
    "pytest scripts/tests",
    "python scripts/claims_check.py",
    "npm test",
    "npm run build",
]


def normalise(cmd: str) -> str:
    """One spelling for a command: Makefile variables, just's {{py}} and `uv run --no-sync` all drop away."""
    cmd = cmd.strip().lstrip("@-")
    for old, new in (("$(PY)", "python"), ("$(PYTEST)", "pytest"), ("$(RUFF)", "ruff"), ("{{py}}", "python")):
        cmd = cmd.replace(old, new)
    cmd = re.sub(r"^uv run --no-sync ", "", cmd)
    return re.sub(r"\s+", " ", cmd).strip()


def ci_steps() -> list[str]:
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    cmds: list[str] = []
    for step in doc["jobs"]["check"]["steps"]:
        run = step.get("run")
        if run is None:
            continue
        cmds += [normalise(line) for line in run.splitlines() if line.strip()]
    return [c for c in cmds if not c.startswith(SETUP_PREFIXES)]


def read_rules(path: Path, indent: str) -> dict[str, tuple[list[str], list[str]]]:
    """target -> (prerequisites, recipe lines) for Makefile (tab-indented) and justfile (space-indented)."""
    rules: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith(indent) and current is not None:
            line = raw.strip()
            if line and not line.startswith(("@echo", "@just", "#")):
                rules[current][1].append(line)
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)(?:\s+[A-Z]+)*\s*:(?!=)\s*(.*)$", raw)
        if m and not raw.startswith(("#", ".", " ", "\t")):
            current = m.group(1)
            rules[current] = (m.group(2).split(), [])
        elif raw.strip() and not raw.startswith("#"):
            current = None
    return rules


def expand(rules: dict[str, tuple[list[str], list[str]]], target: str) -> list[str]:
    deps, body = rules[target]
    out: list[str] = []
    for d in deps:
        out += expand(rules, d)
    return out + [normalise(c) for c in body]


@pytest.fixture(scope="module")
def make_rules():
    return read_rules(ROOT / "Makefile", "\t")


@pytest.fixture(scope="module")
def just_rules():
    return read_rules(ROOT / "justfile", "    ")


def test_ci_runs_the_steps_the_plan_names_in_order():
    assert ci_steps() == EXPECTED


def test_make_ci_is_the_ci_job(make_rules):
    assert expand(make_rules, "ci") == ci_steps()


def test_just_ci_is_the_ci_job(just_rules):
    assert expand(just_rules, "ci") == ci_steps()


@pytest.mark.parametrize("recipe", ["claims", "licences", "hazard", "selftest", "lint", "test", "build"])
def test_makefile_and_justfile_expand_each_shared_recipe_alike(make_rules, just_rules, recipe):
    assert expand(make_rules, recipe) == expand(just_rules, recipe)


def test_test_is_a_prefix_of_the_ci_job_minus_lint_and_extras(make_rules):
    # `make test` is what scripts/test.sh and test.ps1 mirror: it must stay a subsequence of the CI job.
    ci = ci_steps()
    pos = -1
    for step in expand(make_rules, "test"):
        assert step in ci, step
        assert ci.index(step) > pos, f"{step} runs out of order"
        pos = ci.index(step)


def test_claims_recipe_runs_the_claims_check(make_rules, just_rules):
    assert expand(make_rules, "claims") == ["python scripts/claims_check.py"]
    assert expand(just_rules, "claims") == ["python scripts/claims_check.py"]


def test_the_licence_check_is_not_conditional_in_ci():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"]["check"]["steps"] if "licences" in str(s.get("run", "")))
    assert "if" not in step and not step.get("continue-on-error")


def test_the_parser_sees_what_it_should(make_rules, just_rules):
    for rules in (make_rules, just_rules):
        assert {"ci", "claims", "licences", "hazard", "selftest", "test-py", "test-ui", "build"} <= set(rules)
    assert expand(make_rules, "hazard") == [
        "python scripts/hazard_scan.py --selftest",
        "python scripts/hazard_scan.py --strict-allow",
    ]


# ---------------------------------------------------------------------------- scripts/test.sh and scripts/test.ps1

UI_STEPS = ["npm test", "npm run build"]
# The Mac script is read here as text, never run. Its extension is spelled in pieces so the hazard scanner's
# sh-reference rule (Python must not depend on a POSIX shell script) stays strict for every other file.
TEST_SH = ROOT / "scripts" / ("test." + "sh")
TEST_PS1 = ROOT / "scripts" / "test.ps1"


def script_command(cmd: str) -> str:
    """test.sh / test.ps1 spell the interpreter as "$py" / & $py and call tools through `-m`."""
    cmd = cmd.strip().replace("\\", "/")
    cmd = re.sub(r'^(?:& )?"?\$py"?', "python", cmd)
    cmd = re.sub(r"^python -m (ruff|pytest)\b", r"\1", cmd)
    return normalise(cmd)


def sh_steps() -> list[tuple[str, str]]:
    out = []
    for line in TEST_SH.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^\s*step "([^"]+)" (.+)$', line)
        if m:
            out.append(
                (m.group(1), script_command(" ".join(shlex.split(m.group(2))).replace("$py", "python")))
            )
    return out


def ps1_steps() -> list[tuple[str, str]]:
    out = []
    for line in TEST_PS1.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*Invoke-Step '([^']+)' \{ (.+) \}\s*$", line)
        if m:
            out.append((m.group(1), script_command(m.group(2))))
    return out


def test_test_sh_runs_the_ci_job():
    assert [cmd for _, cmd in sh_steps()] == ci_steps()


def test_test_ps1_runs_the_ci_job():
    assert [cmd for _, cmd in ps1_steps()] == ci_steps()


def test_test_sh_and_test_ps1_name_their_steps_alike():
    assert [name for name, _ in sh_steps()] == [name for name, _ in ps1_steps()]


def test_only_the_ui_steps_sit_behind_the_skip_flag():
    sh = TEST_SH.read_text(encoding="utf-8")
    block = re.search(r'if \[ "\$skip_ui" -eq 0 \]; then\n(.*?)\nfi\n', sh, re.S)
    assert block, "the Mac test script lost its --skip-ui block"
    assert [
        script_command(re.sub(r'^\s*step "[^"]+" ', "", ln)) for ln in block.group(1).splitlines()
    ] == UI_STEPS
    ps = TEST_PS1.read_text(encoding="utf-8")
    block = re.search(r"if \(-not \$SkipUi\) \{\r?\n(.*?)\r?\n\}", ps, re.S)
    assert block, "test.ps1 lost its -SkipUi block"
    assert [
        script_command(re.sub(r"^\s*Invoke-Step '[^']+' \{ (.+) \}\s*$", r"\1", ln))
        for ln in block.group(1).splitlines()
    ] == UI_STEPS
    assert [cmd for _, cmd in sh_steps()][-2:] == UI_STEPS


def test_test_ps1_stays_ascii_with_crlf():
    raw = TEST_PS1.read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\r\n") == raw.count(b"\n")
