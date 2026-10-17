"""PLATE app-data and the shared config.json (token and daemon URL), stdlib only.

Resolves the same folder the daemon does: PLATE_HOME if set, else %LOCALAPPDATA%\\Plate on Windows,
~/Library/Application Support/Plate on macOS, ~/.local/share/Plate elsewhere. The file is created
by the daemon (or the installer); the plugin reads it and may add its own keys. Seed: MaxGaffer's
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


def app_data_dir() -> Path:
    env = os.environ.get("PLATE_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Plate"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Plate"
    return Path.home() / ".local" / "share" / "Plate"


def config_path() -> Path:
    env = os.environ.get("PLATE_CONFIG")
    return Path(env) if env else app_data_dir() / "config.json"


def load_config() -> dict[str, Any]:
    """All keys in config.json over the defaults. A missing or unreadable file gives the defaults
    with an empty token (post_packet then refuses to send). utf-8-sig tolerates a PowerShell BOM."""
    cfg: dict[str, Any] = {"token": "", "daemon_url": DEFAULT_DAEMON_URL}
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    return atomic_write_json(config_path(), cfg)


def packets_dir(cfg: dict[str, Any] | None = None) -> Path:
    """Where packet folders are written: config packets_dir (a drive or UNC path) or app-data/packets."""
    cfg = load_config() if cfg is None else cfg
    configured = cfg.get("packets_dir")
    return Path(configured) if configured else app_data_dir() / "packets"
