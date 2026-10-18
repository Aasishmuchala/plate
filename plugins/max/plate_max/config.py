"""PLATE app-data and the shared config.json (token and daemon URL), stdlib only.

Resolves the same folder the daemon does: PLATE_HOME if set, else %LOCALAPPDATA%\\Plate on Windows,
~/Library/Application Support/Plate on macOS, $XDG_DATA_HOME/Plate (~/.local/share/Plate) elsewhere
(what platformdirs.user_data_dir("Plate", appauthor=False) gives the daemon). PLATE_CONFIG moves the
file and PLATE_TOKEN_FILE names a file holding only the token, which wins over config.json's token.
There is one config.json for the daemon and the plugin, keys token, daemon_url, repo_path (the clone
the startup script puts on sys.path) and packets_dir (a drive or UNC path); the file is created by
the daemon (or the installer), the plugin reads it and may add its own keys. The plugin also reads
an optional project_id: the project a scene sends into when its <scene>.plate.json names none (the
dock's binding order is the scene sidecar, then this key, then the .max file's stem). Seed:
MaxGaffer's maxbridge/config.py (stdlib settings under %LOCALAPPDATA%).

The token is checked where it is read (token_problem): 16-256 printable ASCII characters, no
whitespace. A token file holding anything else (a second "# rotated" line, a non-ASCII character)
or a config.json token like that is not used: load_config gives token None (the transport then
refuses to send, with a message that never quotes it) and logs the reason once, as "token source:
PLATE_TOKEN_FILE: ..." or "token source: config.json: ...", never the value. save_config never
writes that None over the file's own token.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_json

DEFAULT_DAEMON_URL = "http://127.0.0.1:4610"
TOKEN_FILE_ENV = "PLATE_TOKEN_FILE"
# The keys config.json is documented to hold; any other key a caller saves is kept as it is.
CONFIG_KEYS = ("token", "daemon_url", "repo_path", "packets_dir")
PROJECT_KEY = "project_id"
# Keys the plugin reads when they are there; nothing requires them and the daemon never writes them.
OPTIONAL_KEYS = (PROJECT_KEY,)
TOKEN_MIN_CHARS = 16
TOKEN_MAX_CHARS = 256
SOURCE_TOKEN_FILE = TOKEN_FILE_ENV
SOURCE_CONFIG = "config.json"
_TOKEN_CHARS = frozenset(chr(c) for c in range(0x21, 0x7F))  # printable ASCII, no space

log = logging.getLogger("plate_max.config")
_reported: set[str] = set()  # each refusal is logged once per session (the poller reads config every tick)


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


def token_problem(token: Any) -> str | None:
    """Why token cannot be the bearer token, or None when it can: text of 16-256 printable ASCII
    characters with no whitespace. The reason never quotes the token."""
    if not isinstance(token, str):
        return f"it is not text ({type(token).__name__})"
    if not TOKEN_MIN_CHARS <= len(token) <= TOKEN_MAX_CHARS:
        return f"it is {len(token)} characters long, not {TOKEN_MIN_CHARS}-{TOKEN_MAX_CHARS}"
    if any(ch.isspace() for ch in token):
        return "it has whitespace in it (a space, a tab or a second line)"
    if not set(token) <= _TOKEN_CHARS:
        return "it has characters that are not printable ASCII"
    return None


def _refuse(source: str, why: str) -> None:
    message = f"token source: {source}: the token is not used because {why}"
    if message not in _reported:
        _reported.add(message)
        log.warning(message)


def _token_file() -> tuple[str | None, str | None]:
    """(token, problem) of PLATE_TOKEN_FILE: (None, None) when the variable is unset or the file is
    unreadable or empty; (None, why) when what it holds is no token (logged, the value never)."""
    path = token_file_path()
    if path is None:
        return None, None
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        return None, None
    if not token:
        return None, None
    why = token_problem(token)
    if why:
        _refuse(SOURCE_TOKEN_FILE, why)
        return None, why
    return token, None


def read_token_file() -> str | None:
    """The token PLATE_TOKEN_FILE holds (stripped; utf-8-sig tolerates a PowerShell BOM), or None
    when the variable is unset, the file is unreadable or empty (config.json's token then applies),
    or what it holds is no token (token_problem; load_config then gives no token at all)."""
    return _token_file()[0]


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
    refuses to send). A token that is no token (token_problem) from the source that wins is None,
    with the reason logged: a malformed token file never falls back to an older config.json token."""
    cfg: dict[str, Any] = {"token": "", "daemon_url": DEFAULT_DAEMON_URL}
    cfg.update(_read_config_file())
    stored = cfg.get("token")
    if stored is None:
        cfg["token"] = ""
    elif stored != "":
        why = token_problem(stored)
        if why:
            _refuse(SOURCE_CONFIG, why)
            cfg["token"] = None
    override, problem = _token_file()
    if override:
        cfg["token"] = override
    elif problem:
        cfg["token"] = None
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """Write config.json. A token that only came from PLATE_TOKEN_FILE is never persisted: the
    file's own token (if any) is written back instead; so is it for a token load_config refused
    (None), which is never written over what the artist has to fix."""
    out = dict(cfg)
    if "token" in out and out["token"] is None:
        stored = _read_config_file().get("token")
        if stored is not None:
            out["token"] = stored
        else:
            out.pop("token")
        return atomic_write_json(config_path(), out)
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


def project_id(cfg: dict[str, Any] | None = None) -> str | None:
    """config.json's optional project_id, stripped; None when it is absent, empty or not a string
    (the dock then falls back to the .max file's stem)."""
    cfg = load_config() if cfg is None else cfg
    value = cfg.get(PROJECT_KEY)
    if not isinstance(value, str):
        return None
    return value.strip() or None
