"""Daemon configuration: where state lives (`Paths`), how the daemon listens (`Settings`), the token.

App data is `platformdirs.user_data_dir("Plate", appauthor=False)` (`%LOCALAPPDATA%\\Plate` on Windows,
`~/Library/Application Support/Plate` on macOS); `PLATE_HOME` moves the folder, `PLATE_CONFIG` moves
`config.json`, and `PLATE_TOKEN_FILE` names a file holding only the token, which wins over
`config.json["token"]`. Inside the home: `config.json`, `index/<project_id>.sqlite` (WAL; never on a
network share, `ensure_local`), `logs/`, `cache/`, `packets/` (the default packet folder) and `projects/`
(where a project lives when the index has no other root registered for it).

`config.json` is one file shared by daemon, Max plugin and installer: `{"token", "daemon_url"}` plus the
plugin's `repo_path` and `packets_dir`. `ensure_config` creates it owner-only with a 32-byte urlsafe token
and adds a missing token or URL to an existing file without touching the keys the plugin owns.

`Settings` binds `bind_host` (default 127.0.0.1) and, when `tailscale_ip` is set, that address too
(`PLATE_BIND=127.0.0.1,<ip>` from `scripts\\start-daemon.ps1 -Tailscale`, or `PLATE_TAILSCALE_IP`); an
unspecified address (0.0.0.0, ::) is refused. The Host allowlist is `127.0.0.1:<port>`, `localhost:<port>`
and the Tailscale address; the Origin allowlist is the same hosts over http, plus the Vite dev server
origins under `PLATE_DEV=1` (Decision 5).
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .fsutil import read_json, write_json_atomic

DEFAULT_PORT = 4610
DEFAULT_BIND = "127.0.0.1"
PACKET_VERSIONS = ["0.1"]
UNSPECIFIED_HOSTS = {"0.0.0.0", "::", "[::]", "", "*"}
DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
MIN_TOKEN_FILE_LEN = 16
SECRET_MODE = 0o600  # config.json holds the token: owner-only from the moment it is created
# <repo>/ui/dist: config.py -> plate_daemon -> daemon -> repo.
REPO_UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"

# Same shape as the packet schema's slug ($defs/slug): lower-case, no path or Windows-forbidden characters.
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ConfigError(RuntimeError):
    pass


class UnsafeDatabaseLocation(RuntimeError):
    """The SQLite index would land on a network share (WAL is unsafe on SMB)."""


# ---------------------------------------------------------------------------- paths


def app_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PLATE_HOME"):
        return Path(env["PLATE_HOME"])
    return Path(platformdirs.user_data_dir("Plate", appauthor=False))


def check_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ValueError(f"invalid project_id {project_id!r}: expected a lower-case slug [a-z0-9_-]")
    return project_id


def index_dir(home: Path | None = None) -> Path:
    return (home or app_home()) / "index"


def index_db_path(project_id: str, home: Path | None = None) -> Path:
    return index_dir(home) / f"{check_project_id(project_id)}.sqlite"


def is_network_path(p: Path) -> bool:
    """True for UNC paths everywhere, and for mapped network drives on Windows."""
    s = str(p)
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        drive = Path(s).drive
        if drive and len(drive) == 2 and drive[1] == ":":
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
    return False


def ensure_local(p: Path) -> Path:
    if is_network_path(p):
        raise UnsafeDatabaseLocation(
            f"{p}: the SQLite index must live on a local disk (WAL is unsafe on SMB); point PLATE_HOME at a local folder"
        )
    return p


def _restrict(path: Path) -> None:
    # The token is a secret: owner-only on POSIX (the umask may have masked the create mode further, never
    # loosened it). On Windows %LOCALAPPDATA% is already per-user and os.chmod only toggles the read-only
    # bit, which 0o600 leaves cleared.
    try:
        os.chmod(path, SECRET_MODE)
    except OSError:
        pass


def new_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_config(config_path: Path, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Read config.json, creating it (or adding a missing token or URL) atomically. Returns the dict."""
    if config_path.exists():
        try:
            cfg = read_json(config_path)
        except ValueError as e:
            raise ConfigError(f"{config_path}: not valid JSON ({e}); fix or delete it") from e
        if not isinstance(cfg, dict):
            raise ConfigError(f"{config_path}: expected a JSON object")
        changed = False
        if not cfg.get("token"):
            cfg["token"] = new_token()
            changed = True
        if not cfg.get("daemon_url"):
            cfg["daemon_url"] = f"http://127.0.0.1:{port}"
            changed = True
        if changed:
            write_json_atomic(config_path, cfg, indent=1, mode=SECRET_MODE)
            _restrict(config_path)
        return cfg
    cfg = {"token": new_token(), "daemon_url": f"http://127.0.0.1:{port}"}
    write_json_atomic(config_path, cfg, indent=1, mode=SECRET_MODE)
    _restrict(config_path)
    return cfg


def read_token_file(path: Path) -> str:
    try:
        # utf-8-sig: a token file written by PowerShell 5.1 may carry a BOM.
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError as e:
        raise ConfigError(f"PLATE_TOKEN_FILE={path}: cannot read it ({e})") from e
    if len(token) < MIN_TOKEN_FILE_LEN:
        raise ConfigError(
            f"PLATE_TOKEN_FILE={path}: the token must be at least {MIN_TOKEN_FILE_LEN} characters"
        )
    return token


@dataclass(frozen=True)
class Paths:
    home: Path
    config_path: Path
    token_file: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        env = os.environ if env is None else env
        home = app_home(env)
        config_path = Path(env["PLATE_CONFIG"]) if env.get("PLATE_CONFIG") else home / "config.json"
        token_file = Path(env["PLATE_TOKEN_FILE"]) if env.get("PLATE_TOKEN_FILE") else None
        return cls(home=home, config_path=config_path, token_file=token_file)

    @property
    def index_dir(self) -> Path:
        return index_dir(self.home)

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def packets_dir(self) -> Path:
        return self.home / "packets"

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    def index_db(self, project_id: str) -> Path:
        return index_db_path(project_id, self.home)

    def ensure_config(self, port: int = DEFAULT_PORT) -> dict[str, Any]:
        return ensure_config(self.config_path, port)

    def token(self, port: int = DEFAULT_PORT) -> str:
        """The bearer token: PLATE_TOKEN_FILE's contents when set, else config.json's `token`."""
        if self.token_file is not None:
            return read_token_file(self.token_file)
        return str(self.ensure_config(port)["token"])


def load_config(paths: Paths | None = None, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """config.json as a dict (created on first use)."""
    return (paths or Paths.from_env()).ensure_config(port)


# ---------------------------------------------------------------------------- settings


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_bind(raw: str | None) -> list[str]:
    hosts = [h.strip() for h in (raw or DEFAULT_BIND).split(",") if h.strip()] or [DEFAULT_BIND]
    for h in hosts:
        if h in UNSPECIFIED_HOSTS:
            raise ConfigError(
                f"refusing to bind {h!r}: PLATE binds loopback (plus an explicit Tailscale address) only"
            )
    return list(dict.fromkeys(hosts))


def _parse_port(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as e:
        raise ConfigError(f"PLATE_PORT={raw!r} is not an integer") from e
    if not 1 <= port <= 65535:
        raise ConfigError(f"PLATE_PORT={port} out of range")
    return port


def _check_tailscale_ip(raw: str) -> str:
    try:
        ip = ipaddress.ip_address(raw.strip().strip("[]"))
    except ValueError as e:
        raise ConfigError(f"tailscale_ip {raw!r} is not an IP address") from e
    if ip.is_unspecified:
        raise ConfigError(f"tailscale_ip {raw!r} is unspecified: never listen on all interfaces")
    return str(ip)


def host_port(host: str, port: int) -> str:
    """`host:port` as it appears in a Host header (IPv6 literals bracketed)."""
    h = host.strip("[]")
    return f"[{h}]:{port}" if ":" in h else f"{h}:{port}"


class Settings(BaseModel):
    home: Path
    port: int = DEFAULT_PORT
    bind_host: str = DEFAULT_BIND
    tailscale_ip: str | None = None
    config_path: Path
    token: str = Field(min_length=1)
    dev: bool = False
    packets_dirs: list[Path] = Field(default_factory=list)
    projects_dir: Path | None = None
    ui_dist: Path = REPO_UI_DIST
    daemon_version: str = __version__
    packet_versions: list[str] = Field(default_factory=lambda: list(PACKET_VERSIONS))
    sync_ingest: bool = False
    cas_copy: bool = False

    @model_validator(mode="after")
    def _defaults(self) -> Settings:
        if self.bind_host in UNSPECIFIED_HOSTS:
            raise ValueError(f"refusing to bind {self.bind_host!r}: never listen on all interfaces")
        if self.tailscale_ip is not None:
            self.tailscale_ip = _check_tailscale_ip(self.tailscale_ip)
        if not self.packets_dirs:
            self.packets_dirs = [self.home / "packets"]
        if self.projects_dir is None:
            self.projects_dir = self.home / "projects"
        return self

    @property
    def bind(self) -> list[str]:
        """Every address the daemon listens on (one pre-bound socket each, one server)."""
        hosts = [self.bind_host]
        if self.tailscale_ip and self.tailscale_ip not in hosts:
            hosts.append(self.tailscale_ip)
        return hosts

    @property
    def host_allowlist(self) -> frozenset[str]:
        hosts = {"127.0.0.1", "localhost", self.bind_host}
        if self.tailscale_ip:
            hosts.add(self.tailscale_ip)
        return frozenset(host_port(h, self.port).lower() for h in hosts)

    @property
    def origin_allowlist(self) -> frozenset[str]:
        origins = {f"http://{h}" for h in self.host_allowlist}
        if self.dev:
            origins.update(DEV_ORIGINS)
        return frozenset(origins)

    @property
    def ui_url(self) -> str:
        """What the daemon prints on start: the UI address with the token in the fragment."""
        return f"http://{host_port(self.bind_host, self.port)}/#token={self.token}"

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        paths = Paths.from_env(env)
        paths.home.mkdir(parents=True, exist_ok=True)
        port = _parse_port(env.get("PLATE_PORT"))
        hosts = parse_bind(env.get("PLATE_BIND"))
        cfg = paths.ensure_config(port)
        token = paths.token(port)
        bind_host, extra = hosts[0], hosts[1:]
        if len(extra) > 1:
            raise ConfigError(
                f"PLATE_BIND={env.get('PLATE_BIND')!r}: at most loopback plus one Tailscale address"
            )
        tailscale = env.get("PLATE_TAILSCALE_IP") or (extra[0] if extra else None) or cfg.get("tailscale_ip")
        if extra and tailscale and _check_tailscale_ip(extra[0]) != _check_tailscale_ip(tailscale):
            raise ConfigError(f"PLATE_BIND names {extra[0]} but PLATE_TAILSCALE_IP is {tailscale}")
        packets_dirs = [paths.packets_dir]
        if cfg.get("packets_dir"):
            packets_dirs.append(Path(str(cfg["packets_dir"])))
        try:
            return cls(
                home=paths.home,
                port=port,
                bind_host=bind_host,
                tailscale_ip=tailscale or None,
                config_path=paths.config_path,
                token=token,
                dev=_env_bool(env, "PLATE_DEV"),
                packets_dirs=list(dict.fromkeys(packets_dirs)),
                projects_dir=paths.projects_dir,
                ui_dist=Path(env["PLATE_UI_DIST"]) if env.get("PLATE_UI_DIST") else REPO_UI_DIST,
                sync_ingest=_env_bool(env, "PLATE_SYNC_INGEST"),
                cas_copy=_env_bool(env, "PLATE_CAS_COPY"),
            )
        except ValueError as e:  # pydantic ValidationError is a ValueError
            raise ConfigError(str(e)) from e
