"""Single-writer lockfile for a project folder (portalocker; works on Windows, macOS and SMB shares).

A project folder may sit on the NAS, where SQLite cannot protect it, so the daemon holds
`<project_dir>/.plate.lock` while the project is open and a second holder (another daemon, another box)
gets `ProjectLocked` immediately. The running daemon also holds the same kind of lock on its app-data
home so two daemons never share one index or one fake-engine journal. The OS releases the lock when the
holder dies, including after `Popen.kill()`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker

LOCK_NAME = ".plate.lock"


class ProjectLocked(RuntimeError):
    """Another process (or another handle in this one) holds the project lock."""


@contextmanager
def project_lock(project_dir: Path, timeout_s: float = 0.0) -> Iterator[portalocker.Lock]:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    lock = portalocker.Lock(
        project_dir / LOCK_NAME, mode="a", timeout=timeout_s, fail_when_locked=True, encoding="utf-8"
    )
    try:
        lock.acquire()
    except (portalocker.AlreadyLocked, portalocker.LockException) as e:
        raise ProjectLocked(f"{project_dir} is locked by another PLATE process ({LOCK_NAME})") from e
    try:
        yield lock
    finally:
        lock.release()
