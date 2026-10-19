"""Single-writer lockfile for a folder (portalocker; works on Windows, macOS and SMB shares).

A project folder may sit on the NAS, where SQLite cannot protect it, so whoever writes a project holds
`<project_dir>/project.lock` (`projects.Lock`) and a second holder (another daemon, another box) gets
`ProjectLocked` at once. The running daemon holds the same kind of lock (`.plate.lock`) on its app-data
home so two daemons never share one index or one fake-engine journal. The OS releases the lock when the
holder dies, including after `Popen.kill()`.

The holder writes `{"host", "pid", "since"}` into the lock file after acquiring it, and the same JSON to
`<lock>.json` beside it: Windows locks the file's bytes, so a second process cannot read a held lock file
there and reads the sidecar instead. `ProjectLocked` carries what it found (None when unreadable).

The lock lasts one write, so it cannot keep a second daemon (another machine on the NAS folder) from writing
the project between ours or from adopting our runs: that is the project lease (`lease.py`, decision 83).
When this process has a lease identity installed (the daemon does; the CLI tools do not), taking
`project.lock` (`PROJECT_LOCK_NAME`) first takes or refreshes `<project>/lease.json`, so another daemon's
live lease raises `lease.ProjectLeased` before anything is written, and the lease is checked again once the
lock is held (two daemons that both found no lease: the one whose lease was overwritten is refused). The
home lock (`LOCK_NAME`) takes no lease.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker

from . import lease
from .fsutil import read_json, write_json_atomic
from .timeutil import iso, utcnow

LOCK_NAME = ".plate.lock"
PROJECT_LOCK_NAME = "project.lock"  # equal to projects.LOCK_NAME (not imported: projects imports this module)


class ProjectLocked(RuntimeError):
    """Another process (or another handle in this one) holds the lock."""

    def __init__(
        self,
        holder_host: str | None = None,
        holder_pid: int | None = None,
        since: str | None = None,
        *,
        path: Path | None = None,
    ):
        self.holder_host, self.holder_pid, self.since, self.path = holder_host, holder_pid, since, path
        who = f"{holder_host or '?'} pid {holder_pid if holder_pid is not None else '?'}"
        when = f" since {since}" if since else ""
        where = f"{path} is" if path else "the folder is"
        super().__init__(f"{where} locked by another PLATE process ({who}{when})")


def _sidecar(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".json")


def holder_info() -> dict[str, object]:
    return {"host": socket.gethostname(), "pid": os.getpid(), "since": iso(utcnow())}


def read_holder(lock_path: Path) -> tuple[str | None, int | None, str | None]:
    """(host, pid, since) of the current holder, from the lock file or its sidecar; Nones if unknown."""
    for candidate in (lock_path, _sidecar(lock_path)):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig") or "null")
        except (OSError, ValueError):
            continue  # Windows: the held lock file's bytes are locked; try the sidecar
        if isinstance(data, dict):
            pid = data.get("pid")
            return (
                str(data["host"]) if data.get("host") is not None else None,
                pid if isinstance(pid, int) else None,
                str(data["since"]) if data.get("since") is not None else None,
            )
    return None, None, None


@contextmanager
def project_lock(
    project_dir: Path, timeout_s: float = 0.0, *, name: str = LOCK_NAME
) -> Iterator[portalocker.Lock]:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    leased = name == PROJECT_LOCK_NAME and lease.enabled()
    if leased:
        lease.acquire(project_dir)  # another daemon's live lease: ProjectLeased before any write
    path = project_dir / name
    # Mode "w": portalocker opens with "a" and truncates only once the lock is held, so a failed attempt
    # never wipes the holder's JSON.
    lock = portalocker.Lock(path, mode="w", timeout=timeout_s, fail_when_locked=True, encoding="utf-8")
    try:
        fh = lock.acquire()
    except (portalocker.AlreadyLocked, portalocker.LockException) as e:
        host, pid, since = read_holder(path)
        raise ProjectLocked(host, pid, since, path=path) from e
    try:
        if leased:  # a daemon that also found no lease may have written its own since: it won
            other = lease.held_by_other(project_dir)
            if other is not None:
                raise lease.ProjectLeased.of(other, project_dir.absolute())
        info = holder_info()
        fh.write(json.dumps(info))
        fh.flush()
        write_json_atomic(_sidecar(path), info)
        yield lock
    finally:
        try:
            if read_json(_sidecar(path)).get("pid") == os.getpid():
                _sidecar(path).unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            pass
        lock.release()
