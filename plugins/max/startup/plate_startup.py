"""PLATE startup script for 3ds Max 2026, copied to scripts/startup by plugins/max/scripts/install.ps1.

Reads repo_path from the PLATE config.json, puts <repo>/plugins/max on sys.path and imports
plate_max. It registers nothing else yet (the dock and the Send action come later). Any failure is
printed to the MAXScript Listener and swallowed, so a broken install can never stop Max starting.
Stdlib only; it cannot import plate_max.config before sys.path is set, so the path logic is repeated.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _config_path() -> Path:
    env = os.environ.get("PLATE_CONFIG")
    if env:
        return Path(env)
    home = os.environ.get("PLATE_HOME")
    if home:
        return Path(home) / "config.json"
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / "AppData" / "Local") / "Plate" / "config.json"


def _boot() -> None:
    try:
        cfg = json.loads(_config_path().read_text(encoding="utf-8-sig"))
        repo = cfg.get("repo_path")
        if not repo:
            print("[PLATE] repo_path missing from config.json; run plugins/max/scripts/install.ps1")
            return
        plugin_dir = str(Path(repo) / "plugins" / "max")
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        import plate_max

        print(
            "[PLATE] plate_max {} loaded (packet {})".format(
                plate_max.PLUGIN_VERSION, plate_max.PACKET_VERSION
            )
        )
    except Exception as exc:  # never block Max startup
        print("[PLATE] plugin not loaded: {}: {}".format(type(exc).__name__, exc))


_boot()
