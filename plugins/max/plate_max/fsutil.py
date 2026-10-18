"""Windows-safe file writes: temp file in the same folder, then os.replace with retry and backoff
(Windows and Defender hold fresh files briefly). JSON is UTF-8, LF line endings, ensure_ascii off,
and strict: NaN and infinity are refused (allow_nan=False), because bare NaN / Infinity tokens are
not JSON and a manifest holding one must never be written. remove_tree retries the same way.
Seed: the PLATE shared conventions (spec/PACKET.md section 1); no external code.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

REPLACE_TRIES = 5
REPLACE_FIRST_DELAY_S = 0.05


def dump_json_bytes(obj: Any, indent: int = 1) -> bytes:
    """The exact bytes every PLATE JSON file is written with (hash these, then write them). A NaN or
    an infinity anywhere in obj raises ValueError: they have no JSON form."""
    return (json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False) + "\n").encode("utf-8")


def canonical_json_bytes(obj: Any) -> bytes:
    """Sorted keys, no spaces: the byte form every hash payload (digests, view_hash) is taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix="plate-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        delay = REPLACE_FIRST_DELAY_S
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, target)
                return target
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def atomic_write_json(path: Path | str, obj: Any, indent: int = 1) -> Path:
    return atomic_write_bytes(path, dump_json_bytes(obj, indent=indent))


def remove_tree(path: Path | str) -> bool:
    """shutil.rmtree with the same retry and backoff as the writes (a file Windows or Defender still
    holds). True when the folder is gone afterwards (or never existed)."""
    target = Path(path)
    delay = REPLACE_FIRST_DELAY_S
    for attempt in range(REPLACE_TRIES):
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == REPLACE_TRIES - 1:
                break
            time.sleep(delay)
            delay *= 2
            continue
        return True
    return not target.exists()
