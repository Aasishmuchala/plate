#!/usr/bin/env python3
"""Windows-hazard scanner (BUILD_PLAN decision 30): fails the build on constructs that work on the Mac
and break on the render box (Windows 10/11 x64), or the other way round. Standard library only.

    python scripts/hazard_scan.py                scan the repo (tracked plus untracked-not-ignored files)
    python scripts/hazard_scan.py --selftest     plant every hazard in a temp tree and prove each is caught
    python scripts/hazard_scan.py --list-rules   print the rule ids and what they catch
    python scripts/hazard_scan.py --strict-allow also fail on allowlist entries that no longer match

Python files are parsed with `ast`, so a hazard inside a comment or a docstring is not a hazard. Rules:

    fcntl          `import fcntl` / `from fcntl import ...` (POSIX only: use portalocker)
    fork           `os.fork` / `os.forkpty`
    symlink        `os.symlink` / `Path.symlink_to` (admin or Developer Mode on Windows)
    shell-true     `shell=True` on any call, `os.system`, `os.popen` (quoting differs; pass an argument list)
    open-encoding  text-mode `open()` / `Path.open()` / `read_text()` / `write_text()` without `encoding=`
    os-rename      `os.rename` (fails on Windows when the target exists: use `os.replace`)
    tmp-literal    a string literal holding a hard-coded POSIX temp dir path (use `tempfile` / `tmp_path`)
    colon-filename a ':' inside a generated file name under daemon/ pixels/ gates/ (Windows forbids it)
    sh-reference   a string literal that names a `.sh` file (Python must not depend on a POSIX shell script)
    backslash-path a Windows path literal with backslashes (`C:\\x`, `\\\\nas\\x`, `a\\b`): use pathlib
                   (not checked in test files, which carry Windows-path fixtures on purpose)
    max-path       a path literal over 200 characters, or a tracked file path over 200 characters (MAX_PATH 260)
    crlf-ps1       a `*.ps1` that is not CRLF throughout (Windows PowerShell 5.1)
    gitattributes  `.gitattributes` missing, or it does not pin `*.ps1` to eol=crlf
    parse          a Python file that does not parse (it could not be checked)

Exceptions live in scripts/hazard_allow.txt, one per line: `<path or glob>[:<line>] <rule|*>  # why`.
Exit 0 when clean, 1 with a `file:line: rule: message` list otherwise.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = Path(__file__).resolve().with_name("hazard_allow.txt")
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache"}
MAX_PATH_LITERAL = 200
COLON_DIRS = ("daemon/", "pixels/", "gates/")

RULES = {
    "fcntl": "import of the POSIX-only fcntl module (use portalocker)",
    "fork": "os.fork is POSIX-only",
    "symlink": "symlinks need admin rights or Developer Mode on Windows (copy or hardlink)",
    "shell-true": "shell invocation (shell=True / os.system / os.popen): pass an argument list",
    "open-encoding": "text-mode file access without encoding= (the Windows default is cp1252)",
    "os-rename": "os.rename fails on Windows when the target exists: use os.replace",
    "tmp-literal": "hard-coded POSIX temp path: use tempfile or pytest tmp_path",
    "colon-filename": "':' in a generated file name (Windows forbids it): use 20261017T094107Z style",
    "sh-reference": "Python references a POSIX shell-script file (Windows has no POSIX shell by default)",
    "backslash-path": "path literal with backslashes: build it with pathlib",
    "max-path": f"path longer than {MAX_PATH_LITERAL} characters (Windows MAX_PATH is 260)",
    "crlf-ps1": "a .ps1 file without CRLF line endings throughout",
    "gitattributes": ".gitattributes missing or *.ps1 not pinned to eol=crlf",
    "parse": "Python file did not parse, so it was not checked",
}

# Built from pieces so this file is clean under its own rules.
TMP_RE = re.compile(r"(?<![\w.:-])/" + "tmp" + r"(?:/|$)")
SH_RE = re.compile(r"\." + r"sh(?![A-Za-z0-9_])")
WIN_PATH_RES = (
    re.compile(r"^[A-Za-z]:\\"),  # C:\Users\x
    re.compile(r"^\\\\[^\\]"),  # \\nas\share
    re.compile(r"^(?:[\w.~ -]+\\)+[\w.~ -]+$"),  # plugins\max\x.py (the whole string is a path)
)
PATHLIKE_RE = re.compile(r"^(?:[A-Za-z]:)?(?:[\\/]|\.{1,2}[\\/])?(?:[\w.~@%-]+[\\/])+[\w.~@%-]*$")
EXPR_THEN_COLON_RE = re.compile(
    r"^:(?!\s)"
)  # `}:x` or `}:{y}` in an f-string, but not the message shape `}: x`
FORMAT_COLON_RE = re.compile(r"\{[^{}]*\}:(?!\s)|%[sd]:%?[sd(]")
FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")
NAMEISH_RE = re.compile(r"(name|file|path|dir|stem|slug)", re.I)
PATH_FUNCS = {
    "Path",
    "PurePath",
    "PureWindowsPath",
    "PurePosixPath",
    "join",
    "joinpath",
    "open",
    "mkdir",
    "with_name",
    "with_suffix",
    "write_text",
    "read_text",
    "write_bytes",
    "read_bytes",
    "replace",
    "rename",
    "copy",
    "copy2",
    "copyfile",
    "move",
    "unlink",
    "exists",
    "makedirs",
    "touch",
}
NOT_A_PATH_CALLS = {"print", "debug", "info", "warning", "error", "exception", "critical", "log", "write"}
MODE_CHARS = set("rwxabt+U")
NON_TEXT_RECEIVERS = {"os", "webbrowser", "tarfile", "zipfile", "wave", "Image"}
TEXT_MODULE_RECEIVERS = {"io", "gzip", "codecs", "bz2", "lzma"}


@dataclass(frozen=True)
class Finding:
    path: str  # repo-relative, posix separators
    line: int
    rule: str
    msg: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.msg}"


@dataclass
class AllowEntry:
    pattern: str
    line: int | None
    rule: str
    source_line: int
    used: bool = False

    def covers(self, f: Finding) -> bool:
        if self.rule not in ("*", f.rule):
            return False
        if self.line is not None and self.line != f.line:
            return False
        return fnmatch.fnmatchcase(f.path, self.pattern)


# ---------------------------------------------------------------------------- allowlist and file listing


def load_allow(path: Path) -> list[AllowEntry]:
    entries: list[AllowEntry] = []
    if not path.exists():
        return entries
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split()
        if len(parts) != 2:
            raise SystemExit(f"{path.name}:{no}: expected '<path>[:<line>] <rule>', got {raw!r}")
        target, rule = parts
        if rule != "*" and rule not in RULES:
            raise SystemExit(f"{path.name}:{no}: unknown rule {rule!r} (see --list-rules)")
        line: int | None = None
        m = re.fullmatch(r"(.+):(\d+)", target)
        if m:
            target, line = m.group(1), int(m.group(2))
        entries.append(AllowEntry(target, line, rule, no))
    return entries


def list_files(root: Path) -> list[Path]:
    """Tracked files plus untracked files git does not ignore (new files are scanned before they are committed).
    Without git, every file under root outside the usual cache and dependency folders."""
    names: list[str] = []
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        )
        names = [n for n in proc.stdout.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    out: list[Path] = []
    for n in dict.fromkeys(names):
        p = root / n
        if SKIP_PARTS & set(Path(n).parts) or not p.is_file():  # skip deleted-but-tracked files too
            continue
        out.append(p)
    return sorted(out)


# ---------------------------------------------------------------------------- Python (AST) rules


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    ids.add(id(v))
    return ids


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords) or any(k.arg is None for k in call.keywords)


def _mode_arg(call: ast.Call, pos: int) -> str | None:
    if (
        len(call.args) > pos
        and isinstance(call.args[pos], ast.Constant)
        and isinstance(call.args[pos].value, str)
    ):
        return call.args[pos].value
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
            return k.value.value
    if len(call.args) > pos or any(k.arg == "mode" for k in call.keywords):
        return "?"  # dynamic mode: cannot prove it is binary, so require encoding
    return "r"


def _open_without_encoding(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        is_path_open = False
        receiver = None
    elif isinstance(f, ast.Attribute) and f.attr == "open":
        receiver = f.value.id if isinstance(f.value, ast.Name) else None
        if receiver in NON_TEXT_RECEIVERS:
            return False  # fd/bytes, browser, archive or Pillow opens carry no text encoding
        is_path_open = receiver not in TEXT_MODULE_RECEIVERS
    else:
        return False
    if (
        is_path_open
        and call.args
        and not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str))
    ):
        return False  # x.open(<path>) is a library open (Pillow, zipfile); Path.open takes a mode first
    mode = _mode_arg(call, 0 if is_path_open else 1)
    if mode and mode != "?" and not set(mode) <= MODE_CHARS:
        return False  # the first argument was not a mode string: a custom x.open("name"), not a file open
    return bool(mode) and "b" not in mode and not _has_kw(call, "encoding")


def _is_colon_timestamp(n: ast.AST) -> bool:
    if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
        return False
    if n.func.attr == "isoformat":
        return True
    if n.func.attr == "strftime" and n.args and isinstance(n.args[0], ast.Constant):
        return ":" in str(n.args[0].value)
    return False


def _in_path_context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Is this expression used to build a file name or path? Walk up to the enclosing statement."""
    cur: ast.AST = node
    while cur in parents:
        parent = parents[cur]
        if isinstance(parent, ast.stmt):
            if isinstance(parent, ast.Raise):
                return False
            targets: list[ast.expr] = []
            if isinstance(parent, ast.Assign):
                targets = list(parent.targets)
            elif isinstance(parent, ast.AnnAssign):
                targets = [parent.target]
            names = [
                t.id if isinstance(t, ast.Name) else t.attr
                for t in targets
                if isinstance(t, ast.Name | ast.Attribute)
            ]
            return any(NAMEISH_RE.search(n) for n in names)
        if isinstance(parent, ast.Call):
            name = _call_name(parent)
            if name in NOT_A_PATH_CALLS:
                return False
            if name in PATH_FUNCS:
                return True
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
            return True
        if isinstance(parent, ast.keyword) and parent.arg and NAMEISH_RE.search(parent.arg):
            return True
        cur = parent
    return False


def _colon_name_in_fstring(js: ast.JoinedStr) -> bool:
    values = js.values
    for i, part in enumerate(values[:-1]):
        nxt = values[i + 1]
        if (
            isinstance(part, ast.FormattedValue)
            and isinstance(nxt, ast.Constant)
            and isinstance(nxt.value, str)
            and EXPR_THEN_COLON_RE.match(nxt.value)
        ):
            return True
    return False


def _fstring_ends_with_extension(js: ast.JoinedStr) -> bool:
    last = js.values[-1] if js.values else None
    return (
        isinstance(last, ast.Constant)
        and isinstance(last.value, str)
        and bool(FILE_EXT_RE.search(last.value))
    )


def scan_python(rel: str, source: str) -> list[Finding]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        return [Finding(rel, e.lineno or 1, "parse", f"{e.msg}")]
    out: list[Finding] = []

    def add(node: ast.AST, rule: str, msg: str | None = None) -> None:
        out.append(Finding(rel, getattr(node, "lineno", 1), rule, msg or RULES[rule]))

    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    docs = _docstring_ids(tree)
    check_colon = rel.startswith(COLON_DIRS)
    # Tests carry Windows-path fixtures on purpose (UNC, drive letters, a\\b rejection cases).
    is_test = (
        "tests" in Path(rel).parts or Path(rel).name.startswith("test_") or Path(rel).name == "conftest.py"
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "fcntl" or a.name.startswith("fcntl."):
                    add(node, "fcntl")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "fcntl":
                add(node, "fcntl")
            if node.module == "os":
                for a in node.names:
                    if a.name in ("fork", "forkpty"):
                        add(node, "fork")
                    elif a.name == "symlink":
                        add(node, "symlink")
                    elif a.name in ("rename", "renames"):
                        add(node, "os-rename")
                    elif a.name in ("system", "popen"):
                        add(node, "shell-true", f"os.{a.name} runs a shell")
        elif isinstance(node, ast.Attribute):
            base = node.value.id if isinstance(node.value, ast.Name) else None
            if base == "os":
                if node.attr in ("fork", "forkpty"):
                    add(node, "fork")
                elif node.attr == "symlink":
                    add(node, "symlink")
                elif node.attr in ("rename", "renames"):
                    add(node, "os-rename")
            if node.attr == "symlink_to":
                add(node, "symlink")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "os"
                and f.attr in ("system", "popen")
            ):
                add(node, "shell-true", f"os.{f.attr} runs a shell")
            for k in node.keywords:
                if k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True:
                    add(node, "shell-true")
            if name == "open" and _open_without_encoding(node):
                add(node, "open-encoding", "open() in text mode without encoding=")
            elif (
                name in ("read_text", "write_text")
                and isinstance(f, ast.Attribute)
                and not _has_kw(node, "encoding")
            ):
                add(node, "open-encoding", f"{name}() without encoding=")
            if check_colon and name in PATH_FUNCS:
                for sub in ast.walk(node):
                    if sub is not node and _is_colon_timestamp(sub):
                        add(node, "colon-filename", "':' timestamp (isoformat / strftime) inside a path")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and check_colon:
            for side in (node.left, node.right):
                for sub in ast.walk(side):
                    if _is_colon_timestamp(sub):
                        add(node, "colon-filename", "':' timestamp (isoformat / strftime) joined into a path")
        elif isinstance(node, ast.JoinedStr) and check_colon:
            if _colon_name_in_fstring(node) and (
                _in_path_context(node, parents) or _fstring_ends_with_extension(node)
            ):
                add(node, "colon-filename", "f-string builds a file name with ':' after a value")

        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            s = node.value
            if TMP_RE.search(s):
                add(node, "tmp-literal")
            if SH_RE.search(s):
                add(node, "sh-reference")
            if not is_test and any(rx.search(s) for rx in WIN_PATH_RES):
                add(node, "backslash-path")
            if len(s) > MAX_PATH_LITERAL and PATHLIKE_RE.match(s):
                add(node, "max-path", f"path literal of {len(s)} characters")
            if (
                check_colon
                and FORMAT_COLON_RE.search(s)
                and (FILE_EXT_RE.search(s) or _in_path_context(node, parents))
            ):
                parent = parents.get(node)
                if not isinstance(parent, ast.JoinedStr):  # f-strings are judged whole above
                    add(node, "colon-filename", "format string builds a file name with ':' after a value")
    return out


# ---------------------------------------------------------------------------- repo-level rules


def scan_repo(root: Path, files: Iterable[Path] | None = None) -> list[Finding]:
    found: list[Finding] = []
    listed = list(files) if files is not None else list_files(root)
    for path in listed:
        rel = path.relative_to(root).as_posix()
        if len(rel) > MAX_PATH_LITERAL:
            found.append(Finding(rel, 0, "max-path", f"tracked path of {len(rel)} characters"))
        if path.suffix == ".py":
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                found.append(Finding(rel, 1, "parse", f"cannot read as UTF-8: {e}"))
                continue
            found.extend(scan_python(rel, source))
        elif path.suffix.lower() == ".ps1":
            raw = path.read_bytes()
            bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
            if bare_lf or (raw and not raw.endswith(b"\r\n")):
                first = next((i for i, ln in enumerate(raw.split(b"\n"), 1) if not ln.endswith(b"\r")), 1)
                found.append(
                    Finding(
                        rel,
                        first,
                        "crlf-ps1",
                        f"{bare_lf} LF-only line ending(s); .gitattributes pins *.ps1 to CRLF",
                    )
                )
    attrs = root / ".gitattributes"
    if not attrs.exists():
        found.append(Finding(".gitattributes", 0, "gitattributes", "missing"))
    else:
        text = attrs.read_text(encoding="utf-8")
        pinned = any(
            ln.split()[:1] == ["*.ps1"] and "eol=crlf" in ln.split()
            for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        )
        if not pinned:
            found.append(Finding(".gitattributes", 1, "gitattributes", "no '*.ps1 ... eol=crlf' line"))
    return sorted(found, key=lambda f: (f.path, f.line, f.rule))


def apply_allow(findings: list[Finding], entries: list[AllowEntry]) -> list[Finding]:
    kept = []
    for f in findings:
        hit = [e for e in entries if e.covers(f)]
        for e in hit:
            e.used = True
        if not hit:
            kept.append(f)
    return kept


# ---------------------------------------------------------------------------- selftest


def _plants() -> dict[str, tuple[str, str]]:
    """rule -> (relative path, file text). Strings are assembled so this file stays clean under its own scan."""
    tmp = "/" + "tmp"
    sh = "build." + "sh"
    return {
        "fcntl": ("daemon/a.py", "import f" + "cntl\n"),
        "fork": ("daemon/b.py", "import os\npid = os." + "fork()\n"),
        "symlink": ("daemon/c.py", "import os\nos." + "symlink('a', 'b')\n"),
        "shell-true": ("daemon/d.py", "import subprocess\nsubprocess.run('ls', shell=" + "True)\n"),
        "open-encoding": ("daemon/e.py", "fh = open('x.txt', 'w')\n"),
        "os-rename": ("daemon/f.py", "import os\nos." + "rename('a', 'b')\n"),
        "tmp-literal": ("daemon/g.py", f"p = '{tmp}/plate'\n"),
        "colon-filename": (
            "daemon/h.py",
            "from pathlib import Path\nstamp = 'x'\nq = Path(f'runs/{stamp}:{stamp}.json')\n",
        ),
        "sh-reference": ("daemon/i.py", f"cmd = ['bash', '{sh}']\n"),
        "backslash-path": ("daemon/j.py", "p = 'plugins\\\\max\\\\x.py'\n"),
        "max-path": ("daemon/k.py", "p = '" + "ab/" * 70 + "'\n"),
        "crlf-ps1": ("scripts/x.ps1", "Write-Host 'a'\nWrite-Host 'b'\n"),
        "parse": ("daemon/l.py", "def broken(:\n"),
    }


CLEAN_SAMPLE = '''"""Docstring mentioning @TMP@/x, fcntl and build.@SH@ is fine."""
import os
import tempfile
from pathlib import Path


def ok(src: Path, dst: Path) -> str:
    os.replace(src, dst)
    with open(dst, "rb") as fh:
        fh.read(1)
    with open(dst, "w", encoding="utf-8", newline="\\n") as fh:
        fh.write(f"{src}: moved")
    msg = f"{src.name}: not found"
    name = f"{dst.stem}-{dst.suffix}.json"
    return tempfile.gettempdir() + msg + name  # fcntl and @TMP@/x in a comment are fine
'''


def selftest() -> int:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        files: list[Path] = []

        def put(rel: str, text: str, crlf: bool = False) -> None:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as fh:
                fh.write(text)
            files.append(p)

        put(".gitattributes", "* text=auto eol=lf\n*.ps1 text eol=crlf\n")
        for rel, text in _plants().values():
            put(rel, text)
        put("daemon/clean.py", CLEAN_SAMPLE.replace("@TMP@", "/" + "tmp").replace("@SH@", "s" + "h"))
        put("scripts/ok.ps1", "Write-Host 'a'\n", crlf=True)
        findings = scan_repo(root, files)
        by_path: dict[str, set[str]] = {}
        for f in findings:
            by_path.setdefault(f.path, set()).add(f.rule)
        for rule, (rel, _) in _plants().items():
            if rule not in by_path.get(rel, set()):
                problems.append(f"{rule}: not caught in {rel} (got {sorted(by_path.get(rel, set()))})")
        for clean in ("daemon/clean.py", "scripts/ok.ps1", ".gitattributes"):
            if clean in by_path:
                problems.append(f"false positive on {clean}: {sorted(by_path[clean])}")
        # .gitattributes rule: delete the pin and expect a finding.
        (root / ".gitattributes").unlink()
        if not any(f.rule == "gitattributes" for f in scan_repo(root, files[1:])):
            problems.append("gitattributes: a missing .gitattributes was not caught")
        # allowlist: an entry silences exactly its rule and file.
        entries = [AllowEntry("daemon/g.py", None, "tmp-literal", 1)]
        left = apply_allow([f for f in findings if f.path == "daemon/g.py"], entries)
        if left or not entries[0].used:
            problems.append("allowlist: entry did not silence its finding")
    if problems:
        print("hazard_scan selftest FAILED:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"hazard_scan selftest ok: {len(_plants())} planted hazards caught, clean sample passes")
    return 0


# ---------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows-hazard scanner (BUILD_PLAN decision 30).")
    ap.add_argument("--root", type=Path, default=ROOT, help="repo root (default: this repo)")
    ap.add_argument(
        "--allow", type=Path, default=None, help="allowlist file (default: scripts/hazard_allow.txt)"
    )
    ap.add_argument("--selftest", action="store_true", help="plant each hazard and prove it is caught")
    ap.add_argument("--list-rules", action="store_true")
    ap.add_argument(
        "--strict-allow", action="store_true", help="fail on allowlist entries that match nothing"
    )
    args = ap.parse_args(argv)
    if args.list_rules:
        for rule, why in RULES.items():
            print(f"{rule:15} {why}")
        return 0
    if args.selftest:
        return selftest()
    root = args.root.resolve()
    allow_path = args.allow or (root / "scripts" / "hazard_allow.txt")
    entries = load_allow(allow_path)
    files = list_files(root)
    findings = apply_allow(scan_repo(root, files), entries)
    stale = [e for e in entries if not e.used]
    for f in findings:
        print(f)
    for e in stale:
        where = f"{e.pattern}:{e.line}" if e.line else e.pattern
        print(
            f"{allow_path.name}:{e.source_line}: stale allowlist entry {where} {e.rule} matches nothing",
            file=sys.stderr,
        )
    if findings:
        print(
            f"hazard_scan: {len(findings)} finding(s) in {len({f.path for f in findings})} file(s); fix them or add a justified line to {allow_path.name}"
        )
        return 1
    if stale and args.strict_allow:
        print(f"hazard_scan: {len(stale)} stale allowlist entr{'y' if len(stale) == 1 else 'ies'}")
        return 1
    print(
        f"hazard_scan: clean ({len(files)} files scanned, {len(entries)} allowlist entr{'y' if len(entries) == 1 else 'ies'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
