"""Static checks for the PowerShell and batch scripts (no PowerShell on the Mac, so they are lexed here).

What this proves: every *.ps1 is ASCII with CRLF line endings, its (), {} and [] balance once strings,
here-strings and comments are skipped, `param(...)` is the first statement when a script has one, and it
uses no bash-isms or pwsh-7-only operators (`&&`, `||`, `??`, `?.`, the ternary) that break Windows
PowerShell 5.1. What it does not prove: that a script runs. Runtime is verified on the render box only
(scripts\\bootstrap.ps1, scripts\\test.ps1; see docs/P0_AUDIT.md).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".venv", "node_modules", ".git"}
PS1_FILES = sorted(p for p in ROOT.rglob("*.ps1") if not SKIP_PARTS & set(p.parts))
BAT_FILES = sorted(p for p in ROOT.rglob("*.bat") if not SKIP_PARTS & set(p.parts))
OPEN, CLOSE = "({[", ")}]"
TOKEN_START = set(" \t\r\n;(){}|=,")

BASHISMS = [
    (re.compile(r"&&|\|\|"), "'&&' / '||' are pwsh 7 only (Windows PowerShell 5.1 parse error)"),
    (re.compile(r"\?\?"), "'??' is pwsh 7 only"),
    (re.compile(r"\$\w+\?\."), "'?.' is pwsh 7 only"),
    (re.compile(r"(?<![|\s])\s+\?\s+[^{]+\s:\s"), "ternary 'a ? b : c' is pwsh 7 only"),
    (re.compile(r"^\s*(fi|then|esac|done|elif)\b"), "bash keyword"),
    (re.compile(r"^\s*export\s+\w+="), "bash export"),
    (re.compile(r"^\s*[A-Za-z_]\w*=\S"), "bash-style assignment (PowerShell needs $name = value)"),
    (re.compile(r"/dev/null"), "/dev/null (use $null or Out-Null)"),
    (re.compile(r"\b(sudo|chmod|chown)\b|\brm\s+-rf?\b|\bmkdir\s+-p\b|\bcp\s+-r\b"), "POSIX command"),
    (re.compile(r"\[\[\s|\s\]\]"), "bash test brackets"),
    (re.compile(r"^\s*source\s+\S"), "bash source (use dot-sourcing: . path)"),
]


def lex(text: str) -> tuple[list[str], list[str]]:
    """Return (code_lines, problems). Code lines keep code characters only: strings become '' and
    comments are dropped. Problems are unbalanced or mismatched brackets and unterminated constructs."""
    problems: list[str] = []
    stack: list[tuple[str, int, bool]] = []  # (opener, line, is a "$(" subexpression inside a string)
    modes = ["code"]
    code: list[list[str]] = [[]]
    i, line, n = 0, 1, len(text)

    def emit(ch: str) -> None:
        code[-1].append(ch)

    def newline() -> None:
        nonlocal line
        line += 1
        code.append([])

    while i < n:
        ch, mode = text[i], modes[-1]
        nxt = text[i + 1] if i + 1 < n else ""
        prev = text[i - 1] if i else "\n"
        if ch == "\n":
            newline()
            i += 1
            continue
        if mode == "dq":
            if ch == "`":
                if nxt == "\n":
                    newline()
                i += 2
            elif ch == '"' and nxt == '"':
                i += 2
            elif ch == '"':
                modes.pop()
                emit("'")
                emit("'")
                i += 1
            elif ch == "$" and nxt == "(":
                stack.append(("(", line, True))
                modes.append("code")
                i += 2
            elif ch == "$" and nxt == "{":
                j = text.find("}", i)
                i = n if j < 0 else j + 1
            else:
                i += 1
            continue
        # code mode
        if ch == "`":
            if nxt == "\n":
                newline()
            i += 2
        elif ch == "<" and nxt == "#" and prev in TOKEN_START:
            j = text.find("#>", i + 2)
            if j < 0:
                problems.append(f"line {line}: unterminated <# block comment")
                break
            for _ in range(text.count("\n", i, j)):
                newline()
            i = j + 2
        elif ch == "#" and prev in TOKEN_START:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif ch == "@" and nxt in "'\"" and text[i + 2 : i + 3] in ("\n", "\r"):
            term = "\n" + nxt + "@"
            j = text.find(term, i)
            if j < 0:
                problems.append(f"line {line}: unterminated here-string")
                break
            for _ in range(text.count("\n", i, j + 1)):
                newline()
            emit("'")
            emit("'")
            i = j + len(term)
        elif ch == "'":
            j = i + 1
            while True:
                k = text.find("'", j)
                if k < 0:
                    problems.append(f"line {line}: unterminated '...' string")
                    return ["".join(c) for c in code], problems
                if text[k + 1 : k + 2] == "'":
                    j = k + 2
                    continue
                break
            for _ in range(text.count("\n", i, k)):
                newline()
            emit("'")
            emit("'")
            i = k + 1
        elif ch == '"':
            modes.append("dq")
            i += 1
        elif ch == "$" and nxt == "{":
            j = text.find("}", i)
            emit("$")
            emit("v")
            i = n if j < 0 else j + 1
        elif ch in OPEN:
            stack.append((ch, line, False))
            emit(ch)
            i += 1
        elif ch in CLOSE:
            want = OPEN[CLOSE.index(ch)]
            if not stack:
                problems.append(f"line {line}: '{ch}' closes nothing")
            else:
                opener, at, subexpr = stack.pop()
                if opener != want:
                    problems.append(f"line {line}: '{ch}' closes '{opener}' opened on line {at}")
                if subexpr:
                    modes.pop()
                    i += 1
                    continue
            emit(ch)
            i += 1
        else:
            emit(ch)
            i += 1
    if len(modes) > 1 and modes[-1] == "dq":
        problems.append(f'line {line}: unterminated "..." string')
    for opener, at, _ in stack:
        problems.append(f"line {at}: '{opener}' is never closed")
    return ["".join(c).rstrip("\r") for c in code], problems


def _ids(paths: list[Path]) -> list[str]:
    return [str(p.relative_to(ROOT)) for p in paths]


def test_there_are_scripts_to_check():
    assert len(PS1_FILES) >= 7, _ids(PS1_FILES)


@pytest.mark.parametrize("path", PS1_FILES + BAT_FILES, ids=_ids(PS1_FILES + BAT_FILES))
def test_ascii_and_crlf(path):
    raw = path.read_bytes()
    assert raw.isascii(), "non-ASCII byte: Windows PowerShell 5.1 reads BOM-less files as ANSI"
    assert raw.endswith(b"\r\n"), "missing final CRLF"
    lf_only = raw.count(b"\n") - raw.count(b"\r\n")
    assert lf_only == 0, f"{lf_only} LF-only line endings (.gitattributes pins *.ps1 and *.bat to CRLF)"


@pytest.mark.parametrize("path", PS1_FILES, ids=_ids(PS1_FILES))
def test_ps1_lexes_balanced_without_bashisms(path):
    lines, problems = lex(path.read_bytes().decode("ascii"))
    for no, text in enumerate(lines, 1):
        for rx, why in BASHISMS:
            if rx.search(text):
                problems.append(f"line {no}: {why}: {text.strip()}")
    assert not problems, problems


@pytest.mark.parametrize("path", PS1_FILES, ids=_ids(PS1_FILES))
def test_param_block_comes_first(path):
    lines, _ = lex(path.read_bytes().decode("ascii"))
    statements = [t.strip() for t in lines if t.strip()]
    if not any(re.match(r"param\s*\(", s, re.I) for s in statements):
        return
    first = statements[0]
    if re.match(r"\[CmdletBinding\(", first, re.I):
        first = statements[1]
    assert re.match(r"param\s*\(", first, re.I), f"param(...) must be the first statement, found: {first}"


@pytest.mark.parametrize(
    "src, problem",
    [
        ("if ($a) { Write-Host 'x' \r\n", "never closed"),
        ("$x = @(1, 2))\r\n", "closes nothing"),
        ("$x = (1, 2]\r\n", "closes '('"),
        ("npm ci && npm test\r\n", "pwsh 7 only"),
        ("$y = $a ?? 'b'\r\n", "pwsh 7 only"),
        ("rm -rf node_modules\r\n", "POSIX command"),
        ("FOO=bar\r\n", "bash-style assignment"),
        ('Write-Host "unterminated\r\n', "unterminated"),
    ],
)
def test_the_lexer_catches_planted_hazards(src, problem):
    lines, problems = lex(src)
    for no, text in enumerate(lines, 1):
        for rx, why in BASHISMS:
            if rx.search(text):
                problems.append(f"line {no}: {why}")
    assert any(problem in p for p in problems), problems


@pytest.mark.parametrize(
    "src",
    [
        "Write-Host \"a $(Get-Date -Format 'yyyy') b\" # (\r\n",
        "<#\r\n.SYNOPSIS\r\n  uses { and ( freely\r\n#>\r\nparam([switch]$X)\r\n",
        "$t = @'\r\n{ ( [ unbalanced inside a here-string\r\n'@\r\n",
        "$p = ${env:ProgramFiles(x86)}\r\n",
        "Get-ChildItem | ? { $_.Length -gt 0 }\r\n",
        "$s = 'it''s (fine'\r\n",
        "Start-Process a `\r\n    -Wait\r\n",
    ],
)
def test_the_lexer_accepts_valid_powershell(src):
    lines, problems = lex(src)
    for no, text in enumerate(lines, 1):
        for rx, why in BASHISMS:
            if rx.search(text):
                problems.append(f"line {no}: {why}: {text}")
    assert not problems, problems
