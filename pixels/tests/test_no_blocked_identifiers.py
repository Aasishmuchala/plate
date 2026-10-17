"""CI guard: no blocked model name may appear in an import or a string literal outside licences.py."""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "plate_pixels"
BLOCKED = [
    "unidepth",
    "superpoint",
    "matanyone",
    "vitb",
    "vitl",
    "flux1_dev",
    "flux.1-dev",
    "robust_video_matting",
    "rvm",
]


def hits(source: str) -> list[str]:
    """Blocked tokens found in import names and string literals (docstrings included), case-insensitive."""
    found = []
    for node in ast.walk(ast.parse(source)):
        texts = []
        if isinstance(node, ast.Import):
            texts = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            texts = [node.module or "", *(a.name for a in node.names)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts = [node.value]
        found += [f"{tok!r} in {t[:40]!r}" for t in texts for tok in BLOCKED if tok in t.lower()]
    return found


def test_package_has_no_blocked_identifiers():
    files = [p for p in sorted(PKG.glob("*.py")) if p.name != "licences.py"]
    assert {p.name for p in files} >= {"backends.py", "server.py", "__init__.py", "__main__.py"}
    bad = {p.name: hits(p.read_text(encoding="utf-8")) for p in files}
    assert not any(bad.values()), {k: v for k, v in bad.items() if v}


def test_the_guard_can_fail():
    """A guard that cannot fail proves nothing: plant each class of hit."""
    assert hits("import unidepth")
    assert hits("from x import superpoint")
    assert hits("x = 'FLUX.1-dev'")
    assert hits('"""Uses MatAnyone."""')
    assert hits("name = 'vitl'")
    assert hits("m = 'rvm'")
    assert not hits("import json\nx = 'depth_anything_v2_small'")


def test_the_registry_is_the_only_place_that_names_them():
    text = (PKG / "licences.py").read_text(encoding="utf-8").lower()
    assert all(tok in text for tok in ("unidepth", "superpoint", "matanyone", "flux1_dev", '"rvm"'))
