"""PLATE app-data and the shared config.json (token and daemon URL), stdlib only.

Resolves the same folder the daemon does: PLATE_HOME if set, else %LOCALAPPDATA%\\Plate on Windows,
~/Library/Application Support/Plate on macOS, $XDG_DATA_HOME/Plate (~/.local/share/Plate) elsewhere
(what platformdirs.user_data_dir("Plate", appauthor=False) gives the daemon). PLATE_CONFIG moves the
file and PLATE_TOKEN_FILE names a file holding only the token, which wins over config.json's token.
There is one config.json for the daemon and the plugin, keys token, daemon_url, repo_path (the clone
the startup script puts on sys.path) and packets_dir (a drive or UNC path); the file is created by
the daemon (or the installer), the plugin reads it and may add its own keys. Seed: MaxGaffer's
maxbridge/config.py (stdlib settings under %LOCALAPPDATA%).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_json

DEFAULT_DAEMON_URL = "http://127.0.0.1:4610"
TOKEN_FILE_ENV = "PLATE_TOKEN_FILE"
# The keys config.json is documented to hold; any other key a caller saves is kept as it is.
CONFIG_KEYS = ("token", "daemon_url", "repo_path", "packets_dir")


def app_data_dir() -> Path:
    env = os.environ.get("PLATE_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Plate"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Plate"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "Plate"


def config_path() -> Path:
    env = os.environ.get("PLATE_CONFIG")
    return Path(env) if env else app_data_dir() / "config.json"


def token_file_path() -> Path | None:
    env = os.environ.get(TOKEN_FILE_ENV)
    return Path(env) if env else None


def read_token_file() -> str | None:
    """The token PLATE_TOKEN_FILE holds (stripped; utf-8-sig tolerates a PowerShell BOM), or None
    when the variable is unset or the file is unreadable or empty (config.json's token then applies)."""
    path = token_file_path()
    if path is None:
        return None
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        return None
    return token or None


def _read_config_file() -> dict[str, Any]:
    """config.json as written, with no defaults and no token override (utf-8-sig tolerates a BOM)."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict[str, Any]:
    """All keys in config.json over the defaults, with PLATE_TOKEN_FILE's token winning over the
    file's. A missing or unreadable file gives the defaults with an empty token (post_packet then
    refuses to send)."""
    cfg: dict[str, Any] = {"token": "", "daemon_url": DEFAULT_DAEMON_URL}
    cfg.update(_read_config_file())
    override = read_token_file()
    if override:
        cfg["token"] = override
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """Write config.json. A token that only came from PLATE_TOKEN_FILE is never persisted: the
    file's own token (if any) is written back instead."""
    out = dict(cfg)
    override = read_token_file()
    if override and out.get("token") == override:
        stored = _read_config_file().get("token")
        if stored:
            out["token"] = stored
        else:
            out.pop("token", None)
    return atomic_write_json(config_path(), out)


def packets_dir(cfg: dict[str, Any] | None = None) -> Path:
    """Where packet folders are written: config packets_dir (a drive or UNC path) or app-data/packets."""
    cfg = load_config() if cfg is None else cfg
    configured = cfg.get("packets_dir")
    return Path(configured) if configured else app_data_dir() / "packets"
