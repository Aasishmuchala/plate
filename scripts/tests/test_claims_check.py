"""scripts/claims_check.py proves itself: it passes on this repo's claims.yaml and fails on every way a claim can rot.

scripts/ is outside pytest's testpaths: run with `.venv/bin/pytest scripts/tests -q` (CI and `make ci` do).
The real run of every listed test is the job of `python scripts/claims_check.py`; here the repo is only checked
statically, and a tiny throw-away project exercises the runner (pass, fail, skip, not collected).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "claims_check.py"
spec = importlib.util.spec_from_file_location("claims_check_under_test", SCRIPT)
cc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cc
spec.loader.exec_module(cc)

ROOT = SCRIPT.parent.parent


# ---------------------------------------------------------------------------- this repo


def test_the_repos_claims_file_is_consistent_with_the_documents_and_tests():
    cfg = cc.load_config(ROOT / "claims.yaml")
    assert cc.check_static(ROOT, cfg) == []


def test_the_public_claims_the_plan_names_are_present():
    cfg = cc.load_config(ROOT / "claims.yaml")
    by_id = {c.id: c for c in cfg.claims}
    assert "gates/tests/test_composite_masked.py::test_outside_the_mask_is_the_plate_exactly" in (
        by_id["outside_mask_zero"].tests
    )
    assert any("test_machine_properties.py" in t for t in by_id["quote_before_spend"].tests)
    assert "daemon/tests/test_egress.py::test_nda_packet_capture" in by_id["nda_egress"].tests


def test_every_covered_document_has_claim_bearing_blocks():
    cfg = cc.load_config(ROOT / "claims.yaml")
    for cov in cfg.covered:
        text = cc.read_doc(ROOT, cov.file)
        assert text is not None
        assert len(cc.doc_blocks(text, cov.section)) >= 5, cov.file


# ---------------------------------------------------------------------------- a small world

DOC = """\
# Title

Intro line that makes no claim.

<!-- a comment
spanning two lines -->

## Claims

- **Alpha** is always true.
- Beta holds
  across two lines.

| what | means |
|---|---|
| gamma | is stated here |

```
- not a claim, inside a fence
```

## Other

- Delta is outside the section.
"""


def world(tmp_path: Path, claims: str, tests: dict[str, str] | None = None) -> tuple[Path, Path]:
    (tmp_path / "doc.md").write_text(DOC, encoding="utf-8", newline="\n")
    for rel, body in (tests or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8", newline="\n")
    path = tmp_path / "claims.yaml"
    path.write_text(textwrap.dedent(claims), encoding="utf-8", newline="\n")
    return tmp_path, path


GOOD_TEST = """\
    import pytest

    def test_alpha():
        assert True

    @pytest.mark.parametrize("n", [1, 2])
    def test_beta(n):
        assert n

    class TestGamma:
        def test_it(self):
            assert True
    """

GOOD_CLAIMS = """\
    version: 1
    covered:
      - file: doc.md
        section: Claims
    claims:
      - id: alpha
        says: [{file: doc.md, quote: Alpha is always true}]
        tests: [t/test_a.py::test_alpha]
      - id: beta
        says: [{file: doc.md, quote: Beta holds across two lines}]
        tests: [t/test_a.py::test_beta, "t/test_a.py::test_beta[1]"]
      - id: gamma
        says: [{file: doc.md, quote: is stated here}]
        tests: [t/test_a.py::TestGamma::test_it]
    """


def static(tmp_path: Path, claims: str = GOOD_CLAIMS, tests: dict[str, str] | None = None) -> list[str]:
    root, path = world(tmp_path, claims, {"t/test_a.py": GOOD_TEST} if tests is None else tests)
    return cc.check_static(root, cc.load_config(path))


def test_doc_blocks_are_bullets_rows_and_paragraphs_inside_the_section():
    blocks = [b for _, b in cc.doc_blocks(DOC, "Claims")]
    assert blocks == [
        "- Alpha is always true.",
        "- Beta holds across two lines.",
        "| gamma | is stated here |",
    ]


def test_whole_document_blocks_include_paragraphs_but_not_comments_headings_or_fences():
    text = cc.COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), DOC)
    blocks = [b for _, b in cc.doc_blocks(text)]
    assert "Intro line that makes no claim." in blocks
    assert not any("comment" in b or "fence" in b or b == "Title" for b in blocks)
    assert "- Delta is outside the section." in blocks


def test_a_consistent_world_has_no_problems(tmp_path):
    assert static(tmp_path) == []


def test_a_bullet_without_a_claim_is_reported_with_its_line(tmp_path):
    claims = GOOD_CLAIMS.replace("      - id: alpha\n", "      - id: alpha2\n").replace(
        "quote: Alpha is always true", "quote: Alpha is mostly true"
    )
    problems = static(tmp_path, claims)
    assert any("quote not found" in p and "Alpha is mostly true" in p for p in problems)
    assert any(p.startswith("doc.md:") and "not covered by any claim" in p and "Alpha" in p for p in problems)


def test_a_new_bullet_in_a_covered_document_fails_until_it_has_a_claim(tmp_path):
    root, path = world(tmp_path, GOOD_CLAIMS, {"t/test_a.py": GOOD_TEST})
    doc = root / "doc.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace("- Beta holds", "- We promise a new thing.\n- Beta holds"),
        encoding="utf-8",
        newline="\n",
    )
    problems = cc.check_static(root, cc.load_config(path))
    assert len(problems) == 1 and "We promise a new thing" in problems[0]


def test_a_reworded_document_leaves_the_claim_stale(tmp_path):
    root, path = world(tmp_path, GOOD_CLAIMS, {"t/test_a.py": GOOD_TEST})
    doc = root / "doc.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("stated", "shown"), encoding="utf-8", newline="\n")
    problems = cc.check_static(root, cc.load_config(path))
    assert any("is stated here" in p and "quote not found" in p for p in problems)


def test_an_exemption_covers_a_block_and_a_stale_exemption_is_reported(tmp_path):
    exempt = "    exempt:\n      - file: doc.md\n        text: is stated here\n        why: framing only\n"
    head, rest = GOOD_CLAIMS.split("    claims:\n", 1)
    without_gamma = rest[: rest.index("      - id: gamma")]
    assert static(tmp_path, head + exempt + "    claims:\n" + without_gamma) == []
    stale = exempt.replace("is stated here", "nothing at all")
    problems = static(tmp_path, head + stale + "    claims:\n" + rest)
    assert any("stale" in p and "nothing at all" in p for p in problems)


def test_a_claim_needs_a_quote_a_test_and_a_real_test(tmp_path):
    extra = """\
        - id: lonely
          says: []
          tests: []
        - id: ghost
          says: [{file: doc.md, quote: Delta}]
          tests: [t/test_a.py::test_missing, t/nope.py::test_x, t/test_a.py::TestGamma::test_no]
        - id: alpha
          says: [{file: gone.md, quote: x}]
          tests: [t/test_a.py::test_alpha, t/test_a.py::test_alpha]
        """
    claims = GOOD_CLAIMS.rstrip(" ") + textwrap.indent(textwrap.dedent(extra), " " * 6)
    problems = "\n".join(static(tmp_path, claims))
    assert "lonely: no 'says' quote" in problems and "lonely: no tests" in problems
    assert "t/test_a.py has no test named test_missing" in problems
    assert "file t/nope.py does not exist" in problems
    assert "has no test named TestGamma::test_no" in problems
    assert "duplicate claim id" in problems and "says.file gone.md does not exist" in problems
    assert "the same test is listed twice" in problems


def test_ids_with_brackets_and_classes_split_correctly():
    n = cc.parse_id("a/b.py::TestX::test_y[one::two]")
    assert (n.file, n.names, n.bare_func, n.kind) == (
        "a/b.py",
        ("TestX", "test_y[one::two]"),
        "test_y",
        "pytest",
    )
    v = cc.parse_id("vitest:ui/src/x.test.ts::does a thing; and more")
    assert (v.kind, v.file, v.func) == ("vitest", "ui/src/x.test.ts", "does a thing; and more")
    for bad in ("nofile", "a.txt::test_x", "a.py::", "vitest:a.ts"):
        with pytest.raises(cc.ConfigError):
            cc.parse_id(bad)


def test_a_missing_section_is_a_config_error():
    with pytest.raises(cc.ConfigError, match="no heading"):
        cc.doc_blocks(DOC, "Not there")


def test_load_config_rejects_a_malformed_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("version: 2\n", encoding="utf-8", newline="\n")
    with pytest.raises(cc.ConfigError):
        cc.load_config(p)
    p.write_text("version: 1\nclaims:\n  - id: x\n    says: [{file: a}]\n", encoding="utf-8", newline="\n")
    with pytest.raises(cc.ConfigError):
        cc.load_config(p)


# ---------------------------------------------------------------------------- result mapping

JUNIT = """\
<testsuites><testsuite>
  <testcase classname="t.test_a" name="test_ok"/>
  <testcase classname="t.test_a" name="test_param[1]"/>
  <testcase classname="t.test_a" name="test_param[2]"><failure message="boom">trace</failure></testcase>
  <testcase classname="t.test_a" name="test_skip"><skipped message="no ffmpeg"/></testcase>
  <testcase classname="t.test_a.TestK" name="test_m"/>
  <testcase classname="t.test_a" name="test_err"><error message="setup broke"/></testcase>
</testsuite></testsuites>
"""


def test_junit_results_map_back_to_node_ids():
    ids = [
        cc.parse_id(x)
        for x in (
            "t/test_a.py::test_ok",
            "t/test_a.py::test_param",
            "t/test_a.py::test_param[1]",
            "t/test_a.py::test_skip",
            "t/test_a.py::TestK::test_m",
            "t/test_a.py::test_err",
            "t/test_a.py::test_gone",
            "t/test_other.py::test_ok",
        )
    ]
    got = cc.outcomes_from_junit(ids, cc.parse_junit(JUNIT))
    status = {k: v.status for k, v in got.items()}
    assert status == {
        "t/test_a.py::test_ok": "passed",
        "t/test_a.py::test_param": "failed",  # one parameter failing fails the bare id
        "t/test_a.py::test_param[1]": "passed",
        "t/test_a.py::test_skip": "skipped",
        "t/test_a.py::TestK::test_m": "passed",
        "t/test_a.py::test_err": "failed",
        "t/test_a.py::test_gone": "missing",
        "t/test_other.py::test_ok": "missing",
    }
    assert "boom" in got["t/test_a.py::test_param"].detail and got["t/test_a.py::test_param"].count == 2


def test_vitest_results_map_back_to_titles_and_survive_console_noise():
    report = {
        "testResults": [
            {
                "name": "/work/plate/ui/src/test/canvas.test.tsx",
                "assertionResults": [
                    {"title": "a passes", "status": "passed"},
                    {"title": "a fails", "status": "failed", "failureMessages": ["Error: nope\n  at x"]},
                    {"title": "a is skipped", "status": "pending"},
                ],
            }
        ]
    }
    noisy = "console.log from a test\n" + json.dumps({"numTotalTestSuites": 1, **report}) + "\n"
    files = cc.parse_vitest_json(noisy)
    ids = [
        cc.parse_id(f"vitest:ui/src/test/canvas.test.tsx::{t}")
        for t in ("a passes", "a fails", "a is skipped", "nope")
    ]
    got = cc.outcomes_from_vitest(ids, files)
    assert [got[i.raw].status for i in ids] == ["passed", "failed", "skipped", "missing"]
    assert got[ids[1].raw].detail == "Error: nope"
    with pytest.raises(ValueError):
        cc.parse_vitest_json("no report here")


# ---------------------------------------------------------------------------- the runner, end to end


RUN_TESTS = """\
    import pytest

    def test_good():
        assert True

    def test_bad():
        assert 1 == 2

    def test_skipped():
        pytest.skip("not on this machine")

    @pytest.mark.parametrize("n", [1, 2])
    def test_params(n):
        assert n
    """

RUN_CLAIMS = """\
    version: 1
    claims:
      - id: good
        says: [{file: doc.md, quote: Alpha is always true}]
        tests: [t/test_run.py::test_good, t/test_run.py::test_params]
      - id: bad
        says: [{file: doc.md, quote: Beta holds}]
        tests: [t/test_run.py::test_good, t/test_run.py::test_bad]
      - id: skipped
        says: [{file: doc.md, quote: gamma}]
        tests: [t/test_run.py::test_skipped]
    """


def test_the_runner_passes_what_passes_and_fails_a_failed_or_skipped_test(tmp_path, capsys):
    root, path = world(tmp_path, RUN_CLAIMS, {"t/test_run.py": RUN_TESTS})
    code = cc.main(
        ["--root", str(root), "--claims", str(path), "--only", "good", "--only", "bad", "--only", "skipped"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "ok   good (2 tests)" in out
    assert (
        "FAIL bad" in out
        and "FAILED: t/test_run.py::test_bad" in out
        and "test_good" not in out.split("FAIL bad")[1]
    )
    assert (
        "FAIL skipped" in out
        and "SKIPPED: t/test_run.py::test_skipped" in out
        and "not on this machine" in out
    )
    assert "2 of 3 claims lack a passing test" in out


def test_the_runner_exits_zero_when_every_claim_holds(tmp_path, capsys):
    root, path = world(tmp_path, RUN_CLAIMS, {"t/test_run.py": RUN_TESTS})
    assert cc.main(["--root", str(root), "--claims", str(path), "--only", "good"]) == 0
    assert "all 1 claims hold" in capsys.readouterr().out


def test_a_module_that_does_not_import_is_not_collected_and_fails(tmp_path, capsys):
    root, path = world(
        tmp_path,
        RUN_CLAIMS,
        {"t/test_run.py": "import definitely_not_installed_xyz\n" + textwrap.dedent(RUN_TESTS)},
    )
    assert cc.main(["--root", str(root), "--claims", str(path), "--only", "good"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_static_problems_stop_the_run_before_any_test_starts(tmp_path, capsys):
    root, path = world(
        tmp_path,
        RUN_CLAIMS.replace("test_good, t/test_run.py::test_params", "test_nothing"),
        {"t/test_run.py": RUN_TESTS},
    )
    assert cc.main(["--root", str(root), "--claims", str(path), "--only", "good"]) == 1
    out = capsys.readouterr().out
    assert "has no test named test_nothing" in out and "nothing was run" in out


def test_static_and_list_modes_run_nothing(tmp_path, capsys):
    root, path = world(tmp_path, GOOD_CLAIMS, {"t/test_a.py": GOOD_TEST})
    assert cc.main(["--root", str(root), "--claims", str(path), "--static"]) == 0
    assert "static ok (3 claims" in capsys.readouterr().out
    assert cc.main(["--root", str(root), "--claims", str(path), "--list"]) == 0
    assert "t/test_a.py::TestGamma::test_it" in capsys.readouterr().out
    assert cc.main(["--root", str(root), "--claims", str(path), "--only", "nope"]) == 1


def test_skip_ui_leaves_the_vitest_ids_unrun_but_a_claim_keeps_a_python_test(tmp_path):
    claims = textwrap.dedent(
        """\
        version: 1
        claims:
          - id: mixed
            says: [{file: doc.md, quote: Alpha}]
            tests: [t/test_a.py::test_alpha, "vitest:ui/src/x.test.ts::title"]
          - id: ui_only
            says: [{file: doc.md, quote: Alpha}]
            tests: ["vitest:ui/src/x.test.ts::title"]
        """
    )
    cfg = cc.load_config(world(tmp_path, claims, {"t/test_a.py": GOOD_TEST})[1])
    calls: list[str] = []

    def py(ids):
        calls.append("py")
        return {i.raw: cc.Outcome("passed", "", 1) for i in ids}

    def ui(ids):
        calls.append("ui")
        return {}

    lines, problems = cc.evaluate(cfg, cfg.claims, {"pytest": py, "vitest": ui}, skip_ui=True)
    assert calls == ["py"] and problems == ["ui_only: 1 test(s) not passing"]
    assert "ok   mixed (1 tests)" in lines


def test_a_caveat_is_printed_on_every_run(tmp_path):
    claims = textwrap.dedent(
        """\
        version: 1
        claims:
          - id: careful
            says: [{file: doc.md, quote: Alpha}]
            caveat: >-
              says more
              than the code.
            tests: [t/test_a.py::test_alpha]
        """
    )
    cfg = cc.load_config(world(tmp_path, claims, {"t/test_a.py": GOOD_TEST})[1])
    run = {"pytest": lambda ids: {i.raw: cc.Outcome("passed", "", 1) for i in ids}, "vitest": lambda ids: {}}
    lines, problems = cc.evaluate(cfg, cfg.claims, run, skip_ui=False)
    assert problems == [] and "     caveat: says more than the code." in lines
