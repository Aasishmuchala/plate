"""`python -m plate_daemon`: load settings, take the app-data lock, recover, then serve.

Reads `PLATE_HOME`, `PLATE_CONFIG`, `PLATE_TOKEN_FILE`, `PLATE_PORT` (default 4610), `PLATE_DEV` and
`PLATE_BIND` (default 127.0.0.1; `scripts\\start-daemon.ps1 -Tailscale` passes `127.0.0.1,<tailscale ip>`,
the same as `PLATE_TAILSCALE_IP`). One uvicorn server listens on one pre-bound socket per address (the
loopback one and, only when set, the Tailscale one), so the lifespan, the recovery pass and signal
handling run once. Unspecified addresses (0.0.0.0, ::) are refused even if they arrive through DNS. Once
the sockets are bound it prints `PLATE daemon http://127.0.0.1:4610/#token=<token>` on stdout: open that
URL and the UI keeps the token (Decision 5). `PLATE_FAKE_ENGINE` (JSON of
FakeEngine flags) scripts the in-process fake provider whose journal is `<home>/fake_engine.json`; a
scripted SimulatedCrash exits the process hard, which is what the cross-process crash test relies on.
Before serving, every project index gets `recover_on_boot` (re-poll, adopt or fail; never resubmit) for the
seed runs the fake engine owns. Pipeline runs are skipped there: `Pipeline.resume` in the app lifespan drives
them with their real engine (`StagedEngine` / `AdapterBridge`), so none is re-submitted to the FakeEngine.

The index is a cache of the project folder (Decision 6), so a lost one is rebuilt before that pass
(`rebuild_missing_indexes`, Wave 12, T144): every folder under `settings.projects_dir` whose `runs.jsonl`
exists and whose `index/<id>.sqlite` does not is replayed with `Index.rebuild_from_project` under the
project lock, with a WARNING `index for <id> was missing: rebuilt from the project folder`, and
`recover_on_boot` and `Pipeline.resume` then see its runs. A folder that cannot be read or rebuilt is
logged and skipped; it never keeps the daemon from serving.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, Settings, host_port
from .engine_fake import FakeEngine
from .index import Index
from .lock import ProjectLocked, project_lock
from .projects import Project
from .worker import recover_on_boot

log = logging.getLogger("plate_daemon")
HOME_LOCK_WAIT_S = 10.0
REBUILD_LOCK_WAIT_S = 5.0


def bind_sockets(hosts: list[str], port: int) -> list[socket.socket]:
    socks: list[socket.socket] = []
    try:
        for host in hosts:
            family, socktype, proto, _, addr = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
            )[0]
            if ipaddress.ip_address(addr[0].split("%")[0]).is_unspecified:
                raise ConfigError(f"refusing to bind {host!r} ({addr[0]}): never listen on all interfaces")
            s = socket.socket(family, socktype, proto)
            if sys.platform == "win32":  # pragma: no cover - Windows only
                # Exclusive: no other process may bind the same port and steal requests.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Rebind at once after a crash (TIME_WAIT); POSIX SO_REUSEADDR cannot steal a live listener.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind(addr)
            socks.append(s)
    except BaseException:
        for s in socks:
            s.close()
        raise
    return socks


def _hold_home_lock(stack: ExitStack, home: Path, wait_s: float = HOME_LOCK_WAIT_S) -> None:
    # Windows may take a moment to release a killed process's lock: retry briefly before giving up.
    deadline = time.monotonic() + wait_s
    while True:
        try:
            stack.enter_context(project_lock(home))
            return
        except ProjectLocked:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _rebuild_locked(
    index: Index, project: Project, wait_s: float = REBUILD_LOCK_WAIT_S
) -> dict[str, Any] | None:
    """`rebuild_from_project` under the project lock (retried for `wait_s`: the lock fails at once when held);
    None when the index appeared meanwhile."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with project.lock():
                if index.exists(project.project_id):  # another process rebuilt it while we waited
                    return None
                return index.rebuild_from_project(project.root)
        except ProjectLocked:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def rebuild_missing_indexes(settings: Settings, index: Index) -> list[str]:
    """Rebuild the index of every project folder that has a ledger and no index. Returns the ids rebuilt."""
    pdir = settings.projects_dir
    if pdir is None or not pdir.is_dir():
        return []
    rebuilt: list[str] = []
    for folder in sorted(pdir.iterdir()):
        if not (folder / "runs.jsonl").is_file() or not (folder / "project.json").is_file():
            continue
        try:
            project = Project.load(folder)
            pid = project.project_id
            if index.exists(pid):
                continue
            res = _rebuild_locked(index, project)
            if res is None:
                continue
        except Exception as e:  # unreadable folder, held lock, a replay that cannot commit: serve anyway
            log.warning(
                "index for %s is missing and was not rebuilt: %s: %s", folder.name, type(e).__name__, e
            )
            continue
        log.warning("index for %s was missing: rebuilt from the project folder", pid)
        if res["problems"]:
            log.warning("rebuild of %s: %d problem(s): %s", pid, len(res["problems"]), res["problems"][:5])
        rebuilt.append(pid)
    return rebuilt


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("PLATE_LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(name)s %(message)s"
    )
    try:
        settings = Settings.load()
        engine = FakeEngine.from_env(journal=settings.home / "fake_engine.json")
    except (ConfigError, ValueError) as e:
        print(f"plate_daemon: {e}", file=sys.stderr)
        return 2

    import uvicorn

    from .api import create_app
    from .pipeline import is_pipeline_run

    with ExitStack() as stack:
        try:
            _hold_home_lock(stack, settings.home)
        except ProjectLocked:
            print(f"plate_daemon: another daemon already runs on {settings.home}", file=sys.stderr)
            return 3
        index = Index(settings.home)
        rebuild_missing_indexes(settings, index)
        for project_id in index.project_ids():
            with index.project(project_id) as conn:
                for action in recover_on_boot(conn, engine, skip=is_pipeline_run):
                    log.info("recovery %s: %s", project_id, action)
        app = create_app(settings, engine, crash_exits_process=True)
        try:
            socks = bind_sockets(settings.bind, settings.port)
        except (ConfigError, OSError) as e:
            print(
                f"plate_daemon: cannot listen on {settings.bind} port {settings.port}: {e}", file=sys.stderr
            )
            return 2
        for s in socks:
            stack.callback(s.close)
        print(f"PLATE daemon {settings.ui_url}", flush=True)
        urls = ", ".join(f"http://{host_port(h, settings.port)}" for h in settings.bind)
        log.info("plate_daemon %s on %s (home %s, dev %s)", __version__, urls, settings.home, settings.dev)
        config = uvicorn.Config(
            app,
            log_level=os.environ.get("PLATE_LOG_LEVEL", "info").lower(),
            lifespan="on",
            proxy_headers=False,  # nothing proxies the daemon; never trust X-Forwarded-* from loopback
            server_header=False,
        )
        uvicorn.Server(config).run(sockets=socks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
