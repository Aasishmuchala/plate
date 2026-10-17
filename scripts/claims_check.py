#!/usr/bin/env python3
"""Claims as tests: every public claim PLATE makes is tied to tests that pass, or the build fails.

claims.yaml (repo root) maps each claim in README.md ("What the code enforces") and docs/client-explainer.md
to pytest node ids (and a few vitest titles). This script

  1. checks the file against the repo: every quote still appears in its document, every test id names a test
     that exists, every paragraph / bullet / table row of a covered document is covered by a claim (or exempted
     with a reason), no exemption is stale;
  2. runs the listed tests by node id and fails when any claim has a test that failed, errored, was skipped
     or was not collected.

    python scripts/claims_check.py              static checks, then run every listed test
    python scripts/claims_check.py --static     static checks only (no test is run)
    python scripts/claims_check.py --list       print the claims and their tests
    python scripts/claims_check.py --only ID    run just this claim (repeatable); skips the coverage rule
    python scripts/claims_check.py --skip-ui    do not run the vitest ids (a claim must keep a pytest test)

Exit 0 when every claim holds, 1 otherwise. Needs PyYAML (a project dependency) and, for the vitest ids,
node with the repo's node_modules (npm ci). Standard library otherwise; Windows and macOS alike.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLAIMS_FILE = ROOT / "claims.yaml"
VITEST_PREFIX = "vitest:"
ARG_BUDGET = 16000  # characters of node ids per pytest invocation (Windows allows ~32k in total)
VITEST_MARK = '{"numTotalTestSuites"'

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
ID_SPLIT_RE = re.compile(r"::(?![^\[]*\])")  # '::' that is not inside a [parameter id]


class ConfigError(Exception):
    """claims.yaml is malformed."""


# ---------------------------------------------------------------------------------------------- the file


@dataclass(frozen=True)
class Says:
    file: str
    quote: str


@dataclass
class Claim:
    id: str
    says: list[Says]
    tests: list[str]
    caveat: str = ""


@dataclass
class Exempt:
    file: str
    text: str
    why: str


@dataclass
class Covered:
    file: str
    section: str | None = None


@dataclass
class Config:
    covered: list[Covered] = field(default_factory=list)
    exempt: list[Exempt] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


def norm(text: str) -> str:
    """Plain text for matching: markdown emphasis and code marks dropped, white space collapsed."""
    return re.sub(r"\s+", " ", text.replace("*", "").replace("`", "").replace(" ", " ")).strip()


def _str(v: object, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{where}: expected a non-empty string, got {v!r}")
    return v


def load_config(path: Path) -> Config:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path.name}: cannot read: {e}") from e
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ConfigError(f"{path.name}: expected a mapping with 'version: 1'")
    cfg = Config()
    for i, c in enumerate(doc.get("covered") or []):
        cfg.covered.append(Covered(_str(c.get("file"), f"covered[{i}].file"), c.get("section")))
    for i, e in enumerate(doc.get("exempt") or []):
        cfg.exempt.append(
            Exempt(
                _str(e.get("file"), f"exempt[{i}].file"),
                _str(e.get("text"), f"exempt[{i}].text"),
                _str(e.get("why"), f"exempt[{i}].why"),
            )
        )
    for i, c in enumerate(doc.get("claims") or []):
        cid = _str(c.get("id"), f"claims[{i}].id")
        says = [
            Says(_str(s.get("file"), f"{cid}.says.file"), _str(s.get("quote"), f"{cid}.says.quote"))
            for s in c.get("says") or []
        ]
        tests = [_str(t, f"{cid}.tests") for t in c.get("tests") or []]
        caveat = " ".join(str(c.get("caveat") or "").split())
        cfg.claims.append(Claim(cid, says, tests, caveat))
    return cfg


# ------------------------------------------------------------------------------------------ documents


def read_doc(root: Path, rel: str) -> str | None:
    p = root.joinpath(*rel.split("/"))
    try:
        return COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), p.read_text(encoding="utf-8"))
    except OSError:
        return None


def doc_blocks(text: str, section: str | None = None) -> list[tuple[int, str]]:
    """The claim-bearing blocks of a markdown document: bullets, table rows and paragraphs (headings, table
    headers, separators and code fences are not claims). `section` limits the scan to one heading's body.
    Returns (first line number, normalised text) pairs."""
    lines = text.splitlines()
    lo, hi = 0, len(lines)
    if section is not None:
        level = 0
        start = None
        for n, line in enumerate(lines):
            m = HEADING_RE.match(line)
            if not m:
                continue
            if start is None:
                if norm(m.group(2)) == norm(section):
                    start, level = n + 1, len(m.group(1))
            elif len(m.group(1)) <= level:
                hi = n
                break
        if start is None:
            raise ConfigError(f"no heading named {section!r}")
        lo = start
    blocks: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_no = 0
    in_fence = False

    def flush() -> None:
        nonlocal cur
        if cur:
            blocks.append((cur_no, norm(" ".join(cur))))
        cur = []

    for n in range(lo, hi):
        line = lines[n]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped or HEADING_RE.match(line):
            flush()
        elif stripped.startswith("|"):
            flush()
            nxt = next((x.strip() for x in lines[n + 1 : hi] if x.strip()), "")
            if SEPARATOR_RE.match(stripped) or SEPARATOR_RE.match(nxt):
                continue  # the separator row, and the header row above it
            blocks.append((n + 1, norm(stripped)))
        elif BULLET_RE.match(line):
            flush()
            cur, cur_no = [stripped], n + 1
        else:
            if not cur:
                cur_no = n + 1
            cur.append(stripped)
    flush()
    return blocks


# --------------------------------------------------------------------------------------------- test ids


@dataclass(frozen=True)
class NodeId:
    raw: str
    kind: str  # 'pytest' | 'vitest'
    file: str  # repo-relative, forward slashes
    names: tuple[str, ...]  # pytest: (Class..., func[params]); vitest: (title,)

    @property
    def func(self) -> str:
        return self.names[-1]

    @property
    def bare_func(self) -> str:
        return self.func.split("[", 1)[0]


def parse_id(raw: str) -> NodeId:
    if raw.startswith(VITEST_PREFIX):
        file, sep, title = raw[len(VITEST_PREFIX) :].partition("::")
        if not sep or not title.strip():
            raise ConfigError(f"{raw!r}: a vitest id is 'vitest:<file>::<test title>'")
        return NodeId(raw, "vitest", file, (title,))
    parts = ID_SPLIT_RE.split(raw)
    if len(parts) < 2 or not parts[0].endswith(".py") or not all(parts[1:]):
        raise ConfigError(f"{raw!r}: a pytest id is '<path>.py::<test>' (or '<path>.py::<Class>::<test>')")
    return NodeId(raw, "pytest", parts[0], tuple(parts[1:]))


def _find_test(tree: ast.Module, names: tuple[str, ...]) -> bool:
    body: list[ast.stmt] = tree.body
    for cls in names[:-1]:
        found = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if found is None:
            return False
        body = found.body
    leaf = names[-1].split("[", 1)[0]
    return any(isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == leaf for n in body)


def check_id_exists(root: Path, nid: NodeId) -> str | None:
    path = root.joinpath(*nid.file.split("/"))
    if not path.is_file():
        return f"{nid.raw}: file {nid.file} does not exist"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"{nid.raw}: cannot read {nid.file}: {e}"
    if nid.kind == "vitest":
        return None if nid.func in source else f"{nid.raw}: no test titled {nid.func!r} in {nid.file}"
    try:
        tree = ast.parse(source, filename=nid.file)
    except SyntaxError as e:
        return f"{nid.raw}: {nid.file} does not parse: {e.msg}"
    if not _find_test(tree, nid.names):
        return f"{nid.raw}: {nid.file} has no test named {'::'.join(nid.names)}"
    return None


# --------------------------------------------------------------------------------------- static checks


def check_static(root: Path, cfg: Config, *, coverage: bool = True) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    docs: dict[str, str | None] = {}

    def doc(rel: str) -> str | None:
        if rel not in docs:
            docs[rel] = read_doc(root, rel)
        return docs[rel]

    for c in cfg.claims:
        if c.id in seen:
            problems.append(f"{c.id}: duplicate claim id")
        seen.add(c.id)
        if not c.says:
            problems.append(f"{c.id}: no 'says' quote (a claim must point at the sentence it backs)")
        if not c.tests:
            problems.append(f"{c.id}: no tests (a claim without a test is not a claim)")
        if len(set(c.tests)) != len(c.tests):
            problems.append(f"{c.id}: the same test is listed twice")
        for s in c.says:
            text = doc(s.file)
            if text is None:
                problems.append(f"{c.id}: says.file {s.file} does not exist")
            elif norm(s.quote) not in norm(text):
                problems.append(f"{c.id}: quote not found in {s.file} (reworded or removed?): {s.quote!r}")
        for t in c.tests:
            try:
                nid = parse_id(t)
            except ConfigError as e:
                problems.append(f"{c.id}: {e}")
                continue
            msg = check_id_exists(root, nid)
            if msg:
                problems.append(f"{c.id}: {msg}")
    if not coverage:
        return problems
    for cov in cfg.covered:
        text = doc(cov.file)
        if text is None:
            problems.append(f"covered file {cov.file} does not exist")
            continue
        try:
            blocks = doc_blocks(text, cov.section)
        except ConfigError as e:
            problems.append(f"{cov.file}: {e}")
            continue
        quotes = [norm(s.quote) for c in cfg.claims for s in c.says if s.file == cov.file]
        exempts = [e for e in cfg.exempt if e.file == cov.file]
        used: set[int] = set()
        for no, block in blocks:
            if any(q in block for q in quotes):
                continue
            hit = [i for i, e in enumerate(exempts) if norm(e.text) in block]
            if hit:
                used.update(hit)
                continue
            where = f"{cov.file}:{no}"
            problems.append(
                f"{where}: not covered by any claim (add a claim or an exemption): {block[:90]!r}"
            )
        for i, e in enumerate(exempts):
            if i not in used and not any(norm(e.text) in b for _, b in blocks):
                problems.append(f"exempt entry for {e.file} matches nothing (stale): {e.text!r}")
    return problems


# --------------------------------------------------------------------------------------------- running


@dataclass
class Outcome:
    status: str  # 'passed' | 'failed' | 'skipped' | 'missing'
    detail: str = ""
    count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "passed"


def _dotted(nid: NodeId) -> str:
    return ".".join([nid.file[:-3].replace("/", "."), *nid.names[:-1]])


def parse_junit(xml_text: str) -> list[tuple[str, str, str, str]]:
    """(classname, name, status, message) per test case in a pytest junit file."""
    out: list[tuple[str, str, str, str]] = []
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        status, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
                first = (child.get("message") or child.text or "").strip().splitlines()
                message = first[0] if first else child.tag
                break
            if child.tag == "skipped":
                status, message = "skipped", (child.get("message") or "").strip()
                break
        out.append((case.get("classname") or "", case.get("name") or "", status, message))
    return out


def outcomes_from_junit(ids: list[NodeId], cases: list[tuple[str, str, str, str]]) -> dict[str, Outcome]:
    res: dict[str, Outcome] = {}
    for nid in ids:
        cls = _dotted(nid)
        mine = [
            c
            for c in cases
            if c[0] == cls and (c[1] == nid.func or ("[" not in nid.func and c[1].startswith(nid.func + "[")))
        ]
        if not mine:
            res[nid.raw] = Outcome("missing", "no result: not collected, or its module did not import")
            continue
        failed = [c for c in mine if c[2] == "failed"]
        skipped = [c for c in mine if c[2] == "skipped"]
        if failed:
            res[nid.raw] = Outcome("failed", f"{failed[0][1]}: {failed[0][3]}"[:300], len(mine))
        elif skipped:
            res[nid.raw] = Outcome("skipped", f"{skipped[0][1]}: {skipped[0][3]}"[:300], len(mine))
        else:
            res[nid.raw] = Outcome("passed", "", len(mine))
    return res


def _batches(ids: list[NodeId]) -> Iterable[list[NodeId]]:
    batch: list[NodeId] = []
    size = 0
    for nid in ids:
        if batch and size + len(nid.raw) > ARG_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(nid)
        size += len(nid.raw) + 1
    if batch:
        yield batch


def run_pytest(root: Path, ids: list[NodeId]) -> dict[str, Outcome]:
    """Run the node ids with the interpreter running this script; one junit file per batch."""
    results: dict[str, Outcome] = {}
    env = {**os.environ, "PYTHONUTF8": "1"}
    with tempfile.TemporaryDirectory() as td:
        for n, batch in enumerate(_batches(ids)):
            junit = Path(td) / f"claims-junit-{n}.xml"
            cmd = [sys.executable, "-m", "pytest", *[b.raw for b in batch]]
            cmd += ["--junitxml", str(junit), "-p", "no:cacheprovider", "-q"]
            proc = subprocess.run(
                cmd, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            cases: list[tuple[str, str, str, str]] = []
            if junit.is_file():
                try:
                    cases = parse_junit(junit.read_text(encoding="utf-8"))
                except ET.ParseError:
                    pass
            got = outcomes_from_junit(batch, cases)
            if not cases or proc.returncode in (2, 4, 5):
                tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-12:])
                for o in got.values():
                    if o.status == "missing":
                        o.detail = f"pytest exited {proc.returncode} with no result for it:\n{tail}"
            results.update(got)
    return results


def find_vitest(root: Path) -> Path | None:
    for base in (root / "ui" / "node_modules", root / "node_modules"):
        p = base / "vitest" / "vitest.mjs"
        if p.is_file():
            return p
    return None


def parse_vitest_json(stdout: str) -> list[dict]:
    at = stdout.find(VITEST_MARK)
    if at < 0:
        raise ValueError("no vitest JSON report in the output")
    doc, _ = json.JSONDecoder().raw_decode(stdout[at:])
    return list(doc.get("testResults") or [])


def outcomes_from_vitest(ids: list[NodeId], files: list[dict]) -> dict[str, Outcome]:
    res: dict[str, Outcome] = {}
    for nid in ids:
        want = nid.file.replace("\\", "/")
        hits = []
        for f in files:
            if (
                str(f.get("name", ""))
                .replace("\\", "/")
                .endswith(want[3:] if want.startswith("ui/") else want)
            ):
                hits += [a for a in f.get("assertionResults") or [] if a.get("title") == nid.func]
        if not hits:
            res[nid.raw] = Outcome("missing", "vitest reported no test with that title")
        elif any(a.get("status") == "failed" for a in hits):
            first = next(a for a in hits if a.get("status") == "failed")
            msg = (first.get("failureMessages") or [""])[0].strip().splitlines()
            res[nid.raw] = Outcome("failed", msg[0] if msg else "failed", len(hits))
        elif any(a.get("status") != "passed" for a in hits):
            res[nid.raw] = Outcome("skipped", str(hits[0].get("status")), len(hits))
        else:
            res[nid.raw] = Outcome("passed", "", len(hits))
    return res


def run_vitest(root: Path, ids: list[NodeId]) -> dict[str, Outcome]:
    """One vitest run over the files the ids name (node + vitest.mjs directly: no npm.cmd on Windows)."""
    node, vitest = shutil.which("node"), find_vitest(root)
    if node is None or vitest is None:
        why = "node is not on PATH" if node is None else "vitest is not installed (run npm ci)"
        return {i.raw: Outcome("missing", why) for i in ids}
    files = sorted({i.file[3:] if i.file.startswith("ui/") else i.file for i in ids})
    proc = subprocess.run(
        [node, str(vitest), "run", *files, "--reporter=json"],
        cwd=root / "ui",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        report = parse_vitest_json(proc.stdout)
    except (ValueError, json.JSONDecodeError) as e:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
        return {i.raw: Outcome("missing", f"{e}; vitest exited {proc.returncode}:\n{tail}") for i in ids}
    return outcomes_from_vitest(ids, report)


# ------------------------------------------------------------------------------------------- reporting


def evaluate(
    cfg: Config,
    claims: list[Claim],
    runners: dict[str, Callable[[list[NodeId]], dict[str, Outcome]]],
    skip_ui: bool,
) -> tuple[list[str], list[str]]:
    """Run what the claims list; returns (lines to print, problems)."""
    by_kind: dict[str, list[NodeId]] = {"pytest": [], "vitest": []}
    parsed: dict[str, NodeId] = {}
    for c in claims:
        for t in c.tests:
            nid = parse_id(t)
            parsed[t] = nid
            if nid.raw not in {n.raw for n in by_kind[nid.kind]}:
                by_kind[nid.kind].append(nid)
    outcomes: dict[str, Outcome] = {}
    for kind, ids in by_kind.items():
        if not ids or (kind == "vitest" and skip_ui):
            continue
        outcomes.update(runners[kind](ids))
    lines: list[str] = []
    problems: list[str] = []
    for c in claims:
        bad = []
        ran = 0
        for t in c.tests:
            nid = parsed[t]
            if nid.kind == "vitest" and skip_ui:
                continue
            ran += 1
            o = outcomes.get(t) or Outcome("missing", "not run")
            if not o.ok:
                bad.append(f"    {o.status.upper()}: {t}" + (f"\n      {o.detail}" if o.detail else ""))
        if ran == 0:
            bad.append("    no test ran for this claim (all of them are vitest and --skip-ui is set)")
        if bad:
            lines.append(f"FAIL {c.id}")
            lines.extend(bad)
            problems.append(f"{c.id}: {len(bad)} test(s) not passing")
        else:
            lines.append(f"ok   {c.id} ({ran} tests)")
        if c.caveat:
            lines.append(f"     caveat: {c.caveat}")
    return lines, problems


def print_list(cfg: Config) -> None:
    for c in cfg.claims:
        print(f"{c.id}")
        for s in c.says:
            print(f"    says  {s.file}: {s.quote}")
        for t in c.tests:
            print(f"    test  {t}")
        if c.caveat:
            print(f"    caveat  {c.caveat}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Claims as tests: run the tests behind every public claim.")
    ap.add_argument("--root", type=Path, default=ROOT, help="repo root (default: this repo)")
    ap.add_argument("--claims", type=Path, default=None, help="claims file (default: <root>/claims.yaml)")
    ap.add_argument("--static", action="store_true", help="check the file and the documents; run no test")
    ap.add_argument("--list", action="store_true", help="print the claims and their tests")
    ap.add_argument("--only", action="append", default=[], metavar="ID", help="run only this claim")
    ap.add_argument("--skip-ui", action="store_true", help="do not run the vitest ids")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    path = args.claims or (root / "claims.yaml")
    try:
        cfg = load_config(path)
    except ConfigError as e:
        print(f"claims_check: {e}", file=sys.stderr)
        return 1
    if args.list:
        print_list(cfg)
        return 0
    chosen = cfg.claims
    if args.only:
        unknown = sorted(set(args.only) - {c.id for c in cfg.claims})
        if unknown:
            print(f"claims_check: unknown claim id(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        chosen = [c for c in cfg.claims if c.id in args.only]
    problems = check_static(root, cfg, coverage=not args.only)
    for p in problems:
        print(f"claims.yaml: {p}")
    if problems:
        print(f"claims_check: {len(problems)} problem(s) in {path.name}; nothing was run")
        return 1
    n_tests = len({t for c in chosen for t in c.tests})
    if args.static:
        print(f"claims_check: static ok ({len(chosen)} claims, {n_tests} tests named, documents covered)")
        return 0
    runners = {"pytest": lambda ids: run_pytest(root, ids), "vitest": lambda ids: run_vitest(root, ids)}
    lines, failed = evaluate(cfg, chosen, runners, args.skip_ui)
    print("\n".join(lines))
    if failed:
        print(f"claims_check: {len(failed)} of {len(chosen)} claims lack a passing test: {', '.join(failed)}")
        return 1
    print(f"claims_check: all {len(chosen)} claims hold ({n_tests} tests passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
