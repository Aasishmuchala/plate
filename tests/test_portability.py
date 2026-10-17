"""Repo-wide portability lint: PLATE runs natively on Windows 10/11 (the render box, primary runtime)
and macOS. These checks catch the hazards that only bite on the other OS, so they run on the Mac too.

Rules (CLAUDE.md, "Windows and macOS"):
- no hard-coded /tmp or /Users/ paths, no fcntl, os.fork, POSIX-only signals, shell=True or symlinks;
- every text-mode open()/read_text()/write_text() passes encoding=; JSON written with write_text passes newline=;
- no ':' timestamps (strftime with ':' or isoformat()) inside path construction;
- subprocess gets argument lists, never a command string;
- npm scripts are shell-neutral (no rm, cp, mv, export, FOO=bar prefixes, single quotes);
- PowerShell scripts are pure ASCII (Windows PowerShell 5.1 reads BOM-less files as ANSI).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY_DIRS = ["spec", "daemon", "pixels", "engines", "gates", "plugins", "tools", "fixtures", "tests"]
SKIP_PARTS = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".hypothesis", "dist"}
THIS = Path(__file__).resolve()


def _py_files() -> list[Path]:
    out = []
    for d in PY_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if not SKIP_PARTS & set(p.parts) and p.resolve() != THIS:
                out.append(p)
    return sorted(out)


PY_FILES = _py_files()
PY_IDS = [str(p.relative_to(ROOT)) for p in PY_FILES]

FORBIDDEN_TEXT = [
    (re.compile(r"""["']/tmp(/|["'])"""), "hard-coded /tmp: use tempfile or pytest tmp_path"),
    (re.compile(r"""["']/Users/"""), "hard-coded /Users/ path: use Path.home() or a parameter"),
    (re.compile(r"^\s*(import fcntl|from fcntl)", re.M), "fcntl is POSIX-only: use portalocker"),
    (re.compile(r"\bos\.fork\("), "os.fork is POSIX-only"),
    (re.compile(r"\bsignal\.(SIGKILL|SIGHUP|SIGUSR1|SIGUSR2|SIGALRM|SIGCHLD)\b"), "POSIX-only signal"),
    (re.compile(r"shell\s*=\s*True"), "shell=True: pass an argument list"),
    (re.compile(r"\bos\.symlink\(|\.symlink_to\("), "symlinks need admin/Developer Mode on Windows"),
]


@pytest.mark.parametrize("path", PY_FILES, ids=PY_IDS)
def test_no_posix_only_constructs(path):
    text = path.read_text(encoding="utf-8")
    bad = [f"{msg} ({rx.pattern})" for rx, msg in FORBIDDEN_TEXT if rx.search(text)]
    assert not bad, bad


def _kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords) or any(k.arg is None for k in call.keywords)


def _mode(call: ast.Call, pos: int) -> str | None:
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
        return "?"  # dynamic mode: cannot prove binary, require encoding
    return "r"


def _contains_json_dumps(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "dumps":
            return True
    return False


def _is_colon_timestamp(n: ast.AST) -> bool:
    if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
        return False
    if n.func.attr == "isoformat":
        return True
    if n.func.attr == "strftime" and n.args and isinstance(n.args[0], ast.Constant):
        return ":" in str(n.args[0].value)
    return False


PATH_BUILDERS = {"Path", "PurePath", "PureWindowsPath", "join", "open", "mkdir", "with_name", "with_suffix"}


def _encoding_violations(tree: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name == "open" and (
                isinstance(f, ast.Name) or (isinstance(f, ast.Attribute) and f.attr == "open")
            ):
                receiver = (
                    f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
                )
                if receiver in {"os", "webbrowser", "tarfile", "zipfile", "wave", "Image"}:
                    continue  # fd/bytes, browser, archive or Pillow opens: no text encoding involved
                text_module = receiver in {"io", "gzip", "codecs", "bz2", "lzma"}
                is_path_open = isinstance(f, ast.Attribute) and not text_module
                if (
                    is_path_open
                    and n.args
                    and not (isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str))
                ):
                    continue  # x.open(<path>) is a library open (Pillow, zipfile); Path.open takes a mode first
                mode = _mode(n, 0 if is_path_open else 1)
                if mode and "b" not in mode and not _kw(n, "encoding"):
                    out.append(f"line {n.lineno}: open() in text mode without encoding=")
            if (
                name in {"read_text", "write_text"}
                and isinstance(f, ast.Attribute)
                and not _kw(n, "encoding")
            ):
                out.append(f"line {n.lineno}: {name}() without encoding=")
            if name == "write_text" and n.args and _contains_json_dumps(n.args[0]) and not _kw(n, "newline"):
                out.append(f"line {n.lineno}: JSON write_text() without newline='\\n'")
            if name in PATH_BUILDERS:
                for sub in ast.walk(n):
                    if sub is not n and _is_colon_timestamp(sub):
                        out.append(f"line {n.lineno}: ':' timestamp inside a path (Windows forbids ':')")
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
                and f.attr in {"run", "call", "check_call", "check_output", "Popen"}
                and n.args
                and isinstance(n.args[0], (ast.Constant, ast.JoinedStr))
            ):
                out.append(f"line {n.lineno}: subprocess with a command string: pass a list")
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            for side in (n.left, n.right):
                for sub in ast.walk(side):
                    if _is_colon_timestamp(sub):
                        out.append(f"line {n.lineno}: ':' timestamp joined into a path")
    return out


@pytest.mark.parametrize("path", PY_FILES, ids=PY_IDS)
def test_text_io_is_explicit_and_paths_are_windows_safe(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _encoding_violations(tree)
    assert not bad, bad


def test_lint_catches_what_it_claims():
    """The lint is only worth something if it can fail: plant each hazard and expect a hit."""
    samples = {
        "open('x.txt', 'w')": "without encoding",
        "Path('a').read_text()": "read_text() without encoding",
        "p.write_text(json.dumps({}), encoding='utf-8')": "without newline",
        "Path('runs') / datetime.now().isoformat()": "':' timestamp",
        "Path(f'{t.strftime(\"%H:%M\")}.json')": "':' timestamp inside a path",
        "subprocess.run('ffmpeg -i a b')": "command string",
    }
    for src, needle in samples.items():
        hits = _encoding_violations(ast.parse(src))
        assert any(needle in h for h in hits), (src, hits)
    assert any("without encoding" in h for h in _encoding_violations(ast.parse("p.open('w')")))
    assert any("without encoding" in h for h in _encoding_violations(ast.parse("gzip.open(p, 'rt')")))
    clean = "open('x.bin', 'rb'); Path('a').read_text(encoding='utf-8'); Image.open(p); PIL.Image.open(p); z.open(name)"
    assert not _encoding_violations(ast.parse(clean))
    assert FORBIDDEN_TEXT[0][0].search("x = '/tmp/plate'")
    assert FORBIDDEN_TEXT[5][0].search("subprocess.run(cmd, shell=True)")


NPM_BAD = [
    (re.compile(r"(^|[;&|]\s*)(rm|cp|mv|export|cat|mkdir -p)\s"), "POSIX shell command"),
    (re.compile(r"(^|[;&|]\s*)[A-Z_][A-Z0-9_]*=\S+\s"), "FOO=bar env prefix (use cross-env)"),
    (re.compile(r"'"), "single quotes do not quote in cmd.exe"),
]


def _package_jsons() -> list[Path]:
    return [p for p in [ROOT / "package.json", ROOT / "ui" / "package.json"] if p.exists()]


@pytest.mark.parametrize("pkg", _package_jsons(), ids=lambda p: str(p.relative_to(ROOT)))
def test_npm_scripts_are_cross_platform(pkg):
    scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    bad = [f"{name}: {why}: {cmd}" for name, cmd in scripts.items() for rx, why in NPM_BAD if rx.search(cmd)]
    assert not bad, bad


def test_powershell_scripts_are_ascii():
    bad = []
    for p in ROOT.rglob("*.ps1"):
        if SKIP_PARTS & set(p.parts):
            continue
        try:
            p.read_bytes().decode("ascii")
        except UnicodeDecodeError as e:
            bad.append(f"{p.relative_to(ROOT)}: byte {e.start}")
    assert not bad, bad


def _gitattributes() -> dict[str, list[str]]:
    """{pattern: attributes}. gitattributes takes ONE pattern per line; a second pattern on the same line
    is read as an (invalid) attribute name and silently ignored, so it is a failure here."""
    out: dict[str, list[str]] = {}
    for no, line in enumerate((ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pattern, *attrs = line.split()
        bad = [a for a in attrs if not re.fullmatch(r"-?!?[A-Za-z][\w-]*(=\S+)?", a)]
        assert not bad, f".gitattributes line {no}: {bad} are not attributes (one pattern per line)"
        out[pattern] = attrs
    return out


def test_gitattributes_pins_line_endings():
    attrs = _gitattributes()
    assert {"text=auto", "eol=lf"} <= set(attrs["*"])
    for ext in ("ps1", "bat", "ms"):
        assert "eol=crlf" in attrs[f"*.{ext}"], ext
    for ext in ("exr", "png", "jpg", "tif", "tiff", "mp4", "mov", "blend", "sqlite"):
        assert "binary" in attrs[f"*.{ext}"], ext
