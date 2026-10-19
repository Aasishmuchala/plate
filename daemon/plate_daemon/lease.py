"""Project leases (router plan T151, decision 83): one daemon per project folder at a time.

`project.lock` is held per write, so it cannot stop a second daemon (another machine on the same NAS folder,
or a second home on this one) from writing the project between our writes, or from adopting the runs we are
driving when it boots. A lease can: `<project>/lease.json` holds `{host, pid, daemon_id, started_utc,
heartbeat_utc}` of the daemon that first wrote the project, and its `Keeper` thread rewrites `heartbeat_utc`
every `HEARTBEAT_S` (20 s). A lease whose heartbeat is `LEASE_TTL_S` (90 s, `PLATE_LEASE_TTL_S`) old is stale
and the next daemon to write takes it over with a WARNING. A lease written on this host by a pid that is no
longer running is stale at once (a daemon restarted after a crash or a kill -9 must not wait 90 s to resume
its own runs); a pid that is running, or a lease from another host, counts by its heartbeat only.

While another daemon's lease is live, `lock.project_lock` raises `ProjectLeased` before any write to the
project (the API answers 409 `project_leased`), and `Pipeline.resume` and the executor's orphan watch leave
the project's runs to that daemon. Reads never consult the lease.

The identity is per process: `install()` (the API's `create_app` does it once; two apps in one test process
share one identity) turns leases on, `uninstall()` turns them off again (tests). A process that installs none,
the CLI tools (`plate reindex`, `plate-tools purge`), takes no lease and is never refused one. Every lease
file this process wrote is remembered (`held()`), refreshed by `heartbeat()` and removed by `release_all()` at
shutdown. Files are written with `fsutil.write_json_atomic` (temp file + `os.replace` with retry, UTF-8).
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .fsutil import write_json_atomic
from .idempotency import ulid
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

LEASE_NAME = "lease.json"
DEFAULT_TTL_S = 90.0


def _ttl_from_env() -> float:
    raw = os.environ.get("PLATE_LEASE_TTL_S", "90")
    try:
        ttl = float(raw)
    except ValueError:
        ttl = math.nan
    if not math.isfinite(ttl) or ttl <= 0:
        log.warning("PLATE_LEASE_TTL_S=%r is not a positive number of seconds; using %g", raw, DEFAULT_TTL_S)
        return DEFAULT_TTL_S
    return ttl


LEASE_TTL_S = _ttl_from_env()
HEARTBEAT_S = LEASE_TTL_S / 4.5  # 20 s at the default: four heartbeats can be lost before the lease lapses
READ_TRIES = 5  # a lease another process is replacing may be unreadable for an instant (Windows, SMB)
READ_FIRST_DELAY_S = 0.02

__all__ = [
    "HEARTBEAT_S",
    "LEASE_NAME",
    "LEASE_TTL_S",
    "Identity",
    "Keeper",
    "LeaseInfo",
    "ProjectLeased",
    "acquire",
    "enabled",
    "heartbeat",
    "held",
    "held_by_other",
    "identity",
    "install",
    "is_live",
    "read",
    "release_all",
    "uninstall",
]

_FIELDS: tuple[tuple[str, type], ...] = (
    ("host", str),
    ("pid", int),
    ("daemon_id", str),
    ("started_utc", str),
    ("heartbeat_utc", str),
)


@dataclass(frozen=True)
class LeaseInfo:
    host: str
    pid: int
    daemon_id: str
    started_utc: str
    heartbeat_utc: str

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name, _ in _FIELDS}

    @classmethod
    def from_json(cls, obj: Any) -> LeaseInfo | None:
        """None for anything that is not a dict holding the five keys with the right types."""
        if not isinstance(obj, dict):
            return None
        for name, kind in _FIELDS:
            value = obj.get(name)
            if not isinstance(value, kind) or isinstance(value, bool):
                return None
        return cls(**{name: obj[name] for name, _ in _FIELDS})


@dataclass(frozen=True)
class Identity:
    """Who this process is when it writes a lease (the `LeaseInfo` fields less the heartbeat)."""

    host: str
    pid: int
    daemon_id: str
    started_utc: str


class ProjectLeased(RuntimeError):
    """Another daemon's live lease covers the project: a write is refused (the API answers 409)."""

    def __init__(self, host: str, pid: int, daemon_id: str, heartbeat_utc: str, *, path: Path):
        self.host, self.pid, self.daemon_id, self.heartbeat_utc, self.path = (
            host,
            pid,
            daemon_id,
            heartbeat_utc,
            path,
        )
        super().__init__(
            f"{path} is leased by another PLATE daemon ({host} pid {pid}, heartbeat {heartbeat_utc}); "
            f"stop that daemon or wait {LEASE_TTL_S:g} s for its lease to lapse"
        )

    @classmethod
    def of(cls, info: LeaseInfo, path: Path) -> ProjectLeased:
        return cls(info.host, info.pid, info.daemon_id, info.heartbeat_utc, path=path)


_guard = threading.RLock()  # every lease file this process reads to write, writes or removes
_identity: Identity | None = None
_held: set[Path] = set()  # lease files this process wrote and has not released


def _lease_path(project_dir: Path | str) -> Path:
    return Path(project_dir).absolute() / LEASE_NAME


# ---------------------------------------------------------------------------- identity


def install(daemon_id: str | None = None, *, host: str | None = None, pid: int | None = None) -> Identity:
    """Turn leases on for this process with this identity (a new ULID, this host, this pid by default). The
    held set starts empty: leases a previous identity wrote are not this one's."""
    global _identity
    me = Identity(
        host=host if host is not None else socket.gethostname(),
        pid=pid if pid is not None else os.getpid(),
        daemon_id=daemon_id if daemon_id is not None else ulid(),
        started_utc=iso(utcnow()),
    )
    with _guard:
        _identity = me
        _held.clear()
    return me


def uninstall() -> None:
    """Turn leases off again and forget the held files without touching them (tests)."""
    global _identity
    with _guard:
        _identity = None
        _held.clear()


def enabled() -> bool:
    return _identity is not None


def identity() -> Identity | None:
    return _identity


def held() -> list[Path]:
    """The lease files this process holds (what `GET /health` counts as `leases`)."""
    with _guard:
        return sorted(_held)


# ---------------------------------------------------------------------------- reading


def _load(path: Path) -> LeaseInfo | None:
    """The lease in `path`: None when the file is missing or does not hold a lease. An OSError other than a
    missing file is retried briefly (a writer's `os.replace` in flight) and then raised: never guessed."""
    delay = READ_FIRST_DELAY_S
    for attempt in range(READ_TRIES):
        try:
            text = path.read_text(encoding="utf-8-sig")
            break
        except FileNotFoundError:
            return None
        except OSError:
            if attempt == READ_TRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
    try:
        return LeaseInfo.from_json(json.loads(text))
    except ValueError:
        return None


def read(project_dir: Path | str) -> LeaseInfo | None:
    """The project's lease; None when there is none or `lease.json` is unreadable or not a lease."""
    try:
        return _load(_lease_path(project_dir))
    except OSError:
        return None


def _pid_running(pid: int) -> bool:
    """Whether a process with this pid runs on this host. Unknown counts as running (never steal on a guess)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows only; os.kill(pid, 0) would terminate the process there
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists; anything else: it does not
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it runs under another user
        return True
    return True


def is_live(info: LeaseInfo, now: datetime | None = None) -> bool:
    """`now - heartbeat_utc < LEASE_TTL_S`; an unparseable heartbeat is not live, nor is a lease this host's
    `pid` wrote when that pid no longer runs (a crashed daemon)."""
    try:
        beat = parse_iso(info.heartbeat_utc)
    except ValueError:
        return False
    if beat is None:
        return False
    if ((now or utcnow()) - beat).total_seconds() >= LEASE_TTL_S:
        return False
    return not (info.host == socket.gethostname() and not _pid_running(info.pid))


def held_by_other(project_dir: Path | str, now: datetime | None = None) -> LeaseInfo | None:
    """The live lease of another daemon on this project; None when there is none or leases are off."""
    me = _identity
    if me is None:
        return None
    info = read(project_dir)
    if info is None or info.daemon_id == me.daemon_id or not is_live(info, now):
        return None
    return info


# ---------------------------------------------------------------------------- writing


def acquire(project_dir: Path | str, now: datetime | None = None) -> LeaseInfo:
    """Take or refresh this process's lease on the project: no lease, ours or a stale one is (re)written
    with a fresh heartbeat (`started_utc` kept when it was already ours); another daemon's live lease raises
    `ProjectLeased`. A stale takeover is logged as a WARNING naming the old holder."""
    me = _identity
    if me is None:
        raise RuntimeError("no lease identity installed (lease.install())")
    path = _lease_path(project_dir)
    now = now or utcnow()
    with _guard:
        old = _load(path)
        started = iso(now)
        if old is not None:
            if old.daemon_id == me.daemon_id:
                started = old.started_utc
            elif is_live(old, now):
                raise ProjectLeased.of(old, path.parent)
            else:
                log.warning(
                    "took over the stale lease on %s from %s pid %s (last heartbeat %s)",
                    path.parent,
                    old.host,
                    old.pid,
                    old.heartbeat_utc,
                )
        info = LeaseInfo(me.host, me.pid, me.daemon_id, started, iso(now))
        write_json_atomic(path, info.to_json())
        _held.add(path)
    return info


def heartbeat(now: datetime | None = None) -> int:
    """Rewrite `heartbeat_utc` of every held lease whose file still carries our `daemon_id`. A file that now
    carries another daemon's id (a takeover, clock trouble) or none at all is dropped from the held set, with
    a WARNING for the former. Returns how many leases were refreshed."""
    me = _identity
    if me is None:
        return 0
    stamp = iso(now or utcnow())
    refreshed = 0
    for path in held():
        with _guard:
            if path not in _held:  # released meanwhile
                continue
            try:
                cur = _load(path)
            except OSError as e:  # kept: the next beat tries again
                log.warning("lease %s not refreshed: %s: %s", path, type(e).__name__, e)
                continue
            if cur is None:
                log.info("lease %s is gone; this daemon no longer holds it", path)
                _held.discard(path)
                continue
            if cur.daemon_id != me.daemon_id:
                log.warning(
                    "lease on %s now carries daemon %s (%s pid %s): this daemon no longer holds it",
                    path.parent,
                    cur.daemon_id,
                    cur.host,
                    cur.pid,
                )
                _held.discard(path)
                continue
            write_json_atomic(path, replace(cur, heartbeat_utc=stamp).to_json())
            refreshed += 1
    return refreshed


def release_all() -> int:
    """Remove every held lease file that still carries our id; the held set is emptied. Returns how many."""
    me = _identity
    removed = 0
    with _guard:
        paths = sorted(_held)
        _held.clear()
        if me is None:
            return 0
        for path in paths:
            try:
                cur = _load(path)
                if cur is not None and cur.daemon_id == me.daemon_id:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as e:
                log.warning("lease %s not released: %s: %s", path, type(e).__name__, e)
    return removed


class Keeper:
    """A daemon thread running `heartbeat()` every `HEARTBEAT_S`; an exception is logged, never fatal."""

    def __init__(self, interval_s: float | None = None):
        self.interval_s = HEARTBEAT_S if interval_s is None else interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="plate-lease-keeper", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout_s)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                heartbeat()
            except Exception as e:
                log.warning("lease heartbeat failed: %s: %s", type(e).__name__, e)
