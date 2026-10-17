"""Atomic file writes that survive Windows and Defender holding fresh files.

Every durable file the daemon writes (config.json, the fake-engine journal, CAS objects, exports) goes
to a temp file in the destination's own directory and is then moved with `os.replace`, retried with
backoff (5 tries, 50 ms doubling) on `PermissionError`, the Windows rule from the shared conventions.
JSON is written UTF-8 with `newline="\n"` so files are byte-identical on both OSes.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

REPLACE_TRIES = 5
REPLACE_FIRST_DELAY_S = 0.05


def replace_with_retry(
    src: Path, dst: Path, tries: int = REPLACE_TRIES, delay_s: float = REPLACE_FIRST_DELAY_S
) -> None:
    """`os.replace(src, dst)`, retrying on PermissionError (a scanner or reader holds dst or src)."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay_s * (2**attempt))


def temp_sibling(path: Path) -> Path:
    """A unique temp name next to `path` (same directory, so os.replace stays on one volume)."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_json_atomic(path: Path, obj: Any, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(path)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=indent, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    # utf-8-sig: tolerate a BOM written by Windows PowerShell 5.1 (the plugin installer).
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)
