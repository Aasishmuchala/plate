"""Where the daemon keeps its state: app-data home, per-project SQLite index, content-addressed store.

App data comes from `platformdirs.user_data_dir("Plate", appauthor=False)` (`%LOCALAPPDATA%\\Plate` on
Windows, `~/Library/Application Support/Plate` on macOS), overridden by env `PLATE_HOME`. The SQLite
index for a project lives at `<home>/index/<project_id>.sqlite`, never inside the project folder: project
folders may sit on the NAS and SQLite WAL is unsafe on SMB. `ensure_local` refuses UNC and (on Windows)
network-drive locations for the index.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import platformdirs

# Same shape as the packet schema's slug ($defs/slug): lower-case, no path or Windows-forbidden characters.
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class UnsafeDatabaseLocation(RuntimeError):
    """The SQLite index would land on a network share (WAL is unsafe on SMB)."""


def app_home() -> Path:
    env = os.environ.get("PLATE_HOME")
    if env:
        return Path(env)
    return Path(platformdirs.user_data_dir("Plate", appauthor=False))


def check_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ValueError(f"invalid project_id {project_id!r}: expected a lower-case slug [a-z0-9_-]")
    return project_id


def index_dir(home: Path | None = None) -> Path:
    return (home or app_home()) / "index"


def index_db_path(project_id: str, home: Path | None = None) -> Path:
    return index_dir(home) / f"{check_project_id(project_id)}.sqlite"


def cas_root(home: Path | None = None) -> Path:
    return (home or app_home()) / "cas"


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
