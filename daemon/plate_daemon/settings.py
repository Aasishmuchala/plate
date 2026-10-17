"""Daemon settings from the environment plus the shared `<app-data>/config.json`.

`Settings.load()` reads `PLATE_HOME` (app-data root), `PLATE_PORT` (default 4610), `PLATE_BIND`
(comma-separated hosts, default `127.0.0.1`; `0.0.0.0` and `::` are refused), `PLATE_CONFIG` (default
`<home>/config.json`), `PLATE_CAS_COPY` and `PLATE_SYNC_INGEST`. It creates config.json with
`{"token": secrets.token_hex(16), "daemon_url": "http://127.0.0.1:<port>"}` when missing, and adds a token
to an existing file that lacks one without touching the keys the Max plugin and its installer own
(`project_id`, `packets_dir`, `repo_path`). One file, one format, shared by daemon, plugin and installer.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import BaseModel, Field

from . import __version__
from .fsutil import read_json, write_json_atomic
from .paths import app_home

DEFAULT_PORT = 4610
PACKET_VERSIONS = ["0.1"]
UNSPECIFIED_HOSTS = {"0.0.0.0", "::", "[::]", "", "*"}


class ConfigError(RuntimeError):
    pass


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_bind(raw: str | None) -> list[str]:
    hosts = [h.strip() for h in (raw or "127.0.0.1").split(",") if h.strip()] or ["127.0.0.1"]
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


def ensure_config(config_path: Path, port: int = DEFAULT_PORT) -> dict:
    """Read config.json, creating it (or adding a missing token) atomically. Returns the parsed dict."""
    if config_path.exists():
        try:
            cfg = read_json(config_path)
        except ValueError as e:
            raise ConfigError(f"{config_path}: not valid JSON ({e}); fix or delete it") from e
        if not isinstance(cfg, dict):
            raise ConfigError(f"{config_path}: expected a JSON object")
        changed = False
        if not cfg.get("token"):
            cfg["token"] = secrets.token_hex(16)
            changed = True
        if not cfg.get("daemon_url"):
            cfg["daemon_url"] = f"http://127.0.0.1:{port}"
            changed = True
        if changed:
            write_json_atomic(config_path, cfg, indent=1)
            _restrict(config_path)
        return cfg
    cfg = {"token": secrets.token_hex(16), "daemon_url": f"http://127.0.0.1:{port}"}
    write_json_atomic(config_path, cfg, indent=1)
    _restrict(config_path)
    return cfg


def _restrict(path: Path) -> None:
    # The token is a secret: owner-only on POSIX. On Windows %LOCALAPPDATA% is already per-user and
    # os.chmod only toggles the read-only bit, which 0o600 leaves cleared.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Settings(BaseModel):
    home: Path
    port: int = DEFAULT_PORT
    bind: list[str] = Field(default_factory=lambda: ["127.0.0.1"])
    config_path: Path
    token: str
    daemon_version: str = __version__
    packet_versions: list[str] = Field(default_factory=lambda: list(PACKET_VERSIONS))
    sync_ingest: bool = False
    cas_copy: bool = False

    @classmethod
    def load(cls) -> Settings:
        home = app_home()
        home.mkdir(parents=True, exist_ok=True)
        port = _parse_port(os.environ.get("PLATE_PORT"))
        bind = parse_bind(os.environ.get("PLATE_BIND"))
        config_path = (
            Path(os.environ["PLATE_CONFIG"]) if os.environ.get("PLATE_CONFIG") else home / "config.json"
        )
        cfg = ensure_config(config_path, port)
        return cls(
            home=home,
            port=port,
            bind=bind,
            config_path=config_path,
            token=str(cfg["token"]),
            sync_ingest=_env_bool("PLATE_SYNC_INGEST"),
            cas_copy=_env_bool("PLATE_CAS_COPY"),
        )
