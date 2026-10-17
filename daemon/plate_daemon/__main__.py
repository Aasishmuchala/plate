"""`python -m plate_daemon`: load settings, take the app-data lock, recover, then serve.

Reads `PLATE_HOME`, `PLATE_CONFIG`, `PLATE_PORT` (default 4610) and `PLATE_BIND` (comma-separated hosts,
default 127.0.0.1; `scripts\\start-daemon.ps1 -Tailscale` passes `127.0.0.1,<tailscale ip>`). One uvicorn
server listens on one pre-bound socket per host, so the lifespan and signal handling run once. Unspecified
addresses (0.0.0.0, ::) are refused even if they arrive through DNS. `PLATE_FAKE_ENGINE` (JSON of
FakeEngine flags) scripts the in-process fake provider whose journal is `<home>/fake_engine.json`; a
scripted SimulatedCrash exits the process hard, which is what the cross-process crash test relies on.
Before serving, every project index gets `recover_on_boot` (re-poll, adopt or fail; never resubmit).
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

from . import __version__
from .engine_fake import FakeEngine
from .lock import ProjectLocked, project_lock
from .settings import ConfigError, Settings
from .store import Index
from .worker import recover_on_boot

log = logging.getLogger("plate_daemon")
HOME_LOCK_WAIT_S = 10.0


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

    with ExitStack() as stack:
        try:
            _hold_home_lock(stack, settings.home)
        except ProjectLocked:
            print(f"plate_daemon: another daemon already runs on {settings.home}", file=sys.stderr)
            return 3
        index = Index(settings.home)
        for project_id in index.project_ids():
            with index.project(project_id) as conn:
                for action in recover_on_boot(conn, engine):
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
        urls = ", ".join(f"http://{h}:{settings.port}" for h in settings.bind)
        print(f"PLATE daemon {__version__} on {urls} (home {settings.home})", flush=True)
        config = uvicorn.Config(
            app, log_level=os.environ.get("PLATE_LOG_LEVEL", "info").lower(), lifespan="on"
        )
        uvicorn.Server(config).run(sockets=socks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
