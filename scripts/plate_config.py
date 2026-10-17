#!/usr/bin/env python3
"""Create or read PLATE's config.json without importing the daemon (standard library only).

    plate_config.py url      ensure config.json, print http://127.0.0.1:<port>/#token=<token>   (default)
    plate_config.py token    ensure config.json, print the token
    plate_config.py path     print the config.json path

Used by bootstrap.sh and start-daemon.sh, the Mac twins of bootstrap.ps1 and start-daemon.ps1. It resolves
the files the way plate_daemon.config does: PLATE_HOME moves the app-data folder (default: platformdirs'
user_data_dir("Plate", appauthor=False)), PLATE_CONFIG moves config.json, and PLATE_TOKEN_FILE (a file
holding only the token) wins over config.json["token"]. Keys the Max installer owns are left alone.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

DEFAULT_PORT = 4610
MIN_TOKEN_FILE_LEN = 16


def app_home() -> Path:
    if os.environ.get("PLATE_HOME"):
        return Path(os.environ["PLATE_HOME"])
    try:
        import platformdirs

        return Path(platformdirs.user_data_dir("Plate", appauthor=False))
    except ImportError:
        pass
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Plate"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Plate"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "Plate"


def config_path() -> Path:
    if os.environ.get("PLATE_CONFIG"):
        return Path(os.environ["PLATE_CONFIG"])
    return app_home() / "config.json"


def _replace_with_retry(src: Path, dst: Path, tries: int = 5) -> None:
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05 * 2**attempt)


def ensure_config(path: Path, port: int) -> dict:
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError as e:
            raise SystemExit(f"{path}: not valid JSON ({e}); fix or delete it") from e
        if not isinstance(cfg, dict):
            raise SystemExit(f"{path}: expected a JSON object")
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(32)
        changed = True
    if not cfg.get("daemon_url"):
        cfg["daemon_url"] = f"http://127.0.0.1:{port}"
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cfg, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o600)  # the token is a secret: owner-only on POSIX
        except OSError:
            pass
        _replace_with_retry(tmp, path)
    return cfg


def token(port: int) -> str:
    cfg = ensure_config(config_path(), port)
    if os.environ.get("PLATE_TOKEN_FILE"):
        value = Path(os.environ["PLATE_TOKEN_FILE"]).read_text(encoding="utf-8-sig").strip()
        if len(value) < MIN_TOKEN_FILE_LEN:
            raise SystemExit(f"PLATE_TOKEN_FILE: the token must be at least {MIN_TOKEN_FILE_LEN} characters")
        return value
    return str(cfg["token"])


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "url"
    port = int(os.environ.get("PLATE_PORT") or DEFAULT_PORT)
    if what == "path":
        print(config_path())
    elif what == "token":
        print(token(port))
    elif what == "url":
        print(f"http://127.0.0.1:{port}/#token={token(port)}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
