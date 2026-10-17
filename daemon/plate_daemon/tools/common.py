"""Helpers the maintenance tools share: staged hashing copies, retrying deletes, safe relative paths,
and 64-hex scanning.

Everything durable goes through `os.replace` (retried on PermissionError by `fsutil.replace_with_retry`),
text is UTF-8, relative paths in manifests are always `/`-separated, and no file name carries a `:`.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from blake3 import blake3

from ..fsutil import replace_with_retry, temp_sibling

CHUNK = 1 << 20
HEX64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
UNLINK_TRIES = 5
UNLINK_FIRST_DELAY_S = 0.05

__all__ = [
    "HEX64_RE",
    "UnsafePath",
    "copy_hashed",
    "hashes_in_obj",
    "hashes_in_text",
    "iter_strings",
    "rel_posix",
    "rmtree_retry",
    "safe_join",
    "unlink_retry",
]


class UnsafePath(ValueError):
    """A relative path from a manifest that would land outside its folder."""


def iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(k)
            yield from iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v)


def hashes_in_text(text: str) -> set[str]:
    return set(HEX64_RE.findall(text))


def hashes_in_obj(obj: Any) -> set[str]:
    out: set[str] = set()
    for s in iter_strings(obj):
        out.update(HEX64_RE.findall(s))
    return out


def rel_posix(path: Path, root: Path) -> str:
    """`path` relative to `root`, always `/`-separated (what manifests store on both OSes)."""
    return path.relative_to(root).as_posix()


def safe_join(root: Path, rel: str) -> Path:
    """`root / rel` for a manifest's `/`-separated path; refuses anything that could leave `root`
    (absolute paths, drive letters, `..`, backslashes, empty parts, `:`)."""
    if not isinstance(rel, str) or not rel:
        raise UnsafePath(f"empty or non-string path {rel!r}")
    if rel.startswith("/") or "\\" in rel or ":" in rel:
        raise UnsafePath(f"{rel!r}: not a relative, '/'-separated path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"{rel!r}: contains an empty, '.' or '..' part")
    return root.joinpath(*parts)


def copy_hashed(src: Path, dst: Path) -> tuple[str, int]:
    """Copy `src` to `dst` through a temp file in `dst`'s folder, hashing the bytes as they are copied, then
    move it into place with `os.replace` (retried). Returns (blake3 hex, size) of what was copied, which is
    what a file changing under the copy is judged by. The source's modification time is kept so age-based
    tools (the purge) still see when a blob was first stored."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(dst)
    h = blake3()
    n = 0
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for chunk in iter(lambda: fin.read(CHUNK), b""):
                h.update(chunk)
                fout.write(chunk)
                n += len(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        try:
            st = os.stat(src)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass  # a timestamp is nice to have, never a reason to fail a copy
        replace_with_retry(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return h.hexdigest(), n


def unlink_retry(path: Path, tries: int = UNLINK_TRIES, delay_s: float = UNLINK_FIRST_DELAY_S) -> None:
    """Delete a file, retrying with backoff when Windows (a scanner, a viewer) holds it open."""
    for attempt in range(tries):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay_s * (2**attempt))


def rmtree_retry(path: Path) -> None:
    """Remove a folder tree; clears the read-only bit Windows refuses to delete through."""

    def onexc(func: Callable[..., Any], p: str, _exc: BaseException) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onexc=onexc)
