"""Atomic file writes that survive Windows and Defender holding fresh files.

Every durable file the daemon writes (config.json, the fake-engine journal, CAS objects, exports) goes
to a temp file in the destination's own directory and is then moved with `os.replace`, retried with
backoff (5 tries, 50 ms doubling) on `PermissionError`, the Windows rule from the shared conventions.
JSON is written UTF-8 with `newline="\n"` so files are byte-identical on both OSes.

`in_flight` / `is_in_flight` are the liveness marker for a writer that leaves hidden temp files in a client's
folder (router plan T153, decision 87): the writer holds an exclusive `portalocker` lock on a marker file for
as long as its temp files exist, the OS drops the lock when the process dies (kill -9 included), and whoever
sweeps the folder next tells a dead writer's leftovers from a live one's by trying the same lock.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import portalocker
from portalocker.exceptions import AlreadyLocked, LockException

REPLACE_TRIES = 5
REPLACE_FIRST_DELAY_S = 0.05
# A sweeper's probe holds a marker's lock for an instant; the writer that just created it retries this long.
MARKER_TRIES = 50
MARKER_RETRY_S = 0.01


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


def write_json_atomic(path: Path, obj: Any, indent: int | None = 1, *, mode: int | None = None) -> None:
    """`mode` (e.g. 0o600 for a secret) is set when the temp file is created, so the final file never
    exists with looser permissions; on Windows only the read-only bit follows it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(path)
    try:
        if mode is None:
            fh = open(tmp, "w", encoding="utf-8", newline="\n")
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
            fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        with fh:
            json.dump(obj, fh, indent=indent, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


@contextmanager
def in_flight(marker: Path) -> Iterator[Path]:
    """Create `marker` and hold an exclusive non-blocking lock on it for the block; release and unlink it on
    the way out (an unlink error is ignored). While it is held `is_in_flight(marker)` is True, and it stops
    being held the moment this process ends, however it ends. The marker name carries a unique tag, so the
    only other holder it can meet is a sweeper's probe, which lasts an instant: acquiring retries briefly."""
    lock = portalocker.Lock(
        marker, mode="w", flags=portalocker.LOCK_EX | portalocker.LOCK_NB, fail_when_locked=True
    )
    for attempt in range(MARKER_TRIES):
        try:
            lock.acquire()
            break
        except AlreadyLocked:
            if attempt == MARKER_TRIES - 1:
                raise
            time.sleep(MARKER_RETRY_S)
    try:
        yield marker
    finally:
        try:
            lock.release()
        finally:
            try:
                marker.unlink()
            except OSError:
                pass


def is_in_flight(marker: Path) -> bool:
    """False when `marker` is missing; True when it exists and another holder has its lock (or it cannot be
    opened at all: a sweeper must not guess); False when the lock is free, in which case it is released at
    once and the file is left for the caller to remove. The probe never creates the file."""
    lock = portalocker.Lock(
        marker, mode="r+", flags=portalocker.LOCK_EX | portalocker.LOCK_NB, fail_when_locked=True
    )
    try:
        lock.acquire()
    except FileNotFoundError:
        return False
    except (AlreadyLocked, LockException):
        return True
    except OSError:  # a permission error, a folder where the marker should be: not ours to call dead
        return True
    lock.release()
    return False


def read_json(path: Path) -> Any:
    # utf-8-sig: tolerate a BOM written by Windows PowerShell 5.1 (the plugin installer).
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)
