"""The safety trio before plate_max changes a client scene: backup, one undo record, a journal.

Copied from MaxDirector's maxbridge (backup.py, undo.py, journal.py), onto plate_max's runtime door
and app-data folder:

  backup()          saveMaxFile(<name>.plate-backup.<ts>.max, useNewFile=False, quiet=True,
                    clearNeedSaveFlag=False): a true copy that leaves the open file, its path and its
                    dirty flag alone. A failed backup raises: nothing is changed without a net.
  one_undo(label)   explicit theHold Begin/Accept/Cancel (pymxs.undo swallows exceptions and can
                    commit half an operation); an exception cancels exactly this hold, never a blind
                    "max undo" that could roll back the artist's previous action.
  Journal           a JSON breadcrumb per operation under <app data>/logs/journal/, written as it goes
                    so a crash mid-send is diagnosable and recoverable from the backup.

Time stamps in file names never contain ':' (Windows). A scene whose name leaves no room for the
backup suffix next to it (a file name past 255 bytes, a path past Windows' 259 characters) gets its
backup in the temp folder as plate-<hash of the scene path>.plate-backup.<ts>.max, the same hash the
long-name sidecar uses (recipes.scene_key); backup_note says so and Send adds it to the result.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import app_data_dir
from ..fsutil import atomic_write_json
from ..recipes import fits_filesystem, scene_key
from ..rt import runtime
from .scene import rt_attr, scene_path

BACKUP_INFIX = ".plate-backup."


class BackupFailed(RuntimeError):
    """saveMaxFile did not produce the backup copy; the operation must not go ahead."""


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _natural_backup_path(scene: str | Path | None, stamp: str) -> tuple[Path, bool]:
    """(<scene without .max>.plate-backup.<ts>.max next to the scene, or the untitled one in the temp
    folder; whether the scene's own folder was used)."""
    source = Path(str(scene)) if scene else None
    if source is not None and source.name and source.parent.is_dir():
        base = source.with_suffix("") if source.suffix.lower() == ".max" else source
        beside = True
    else:
        base = Path(tempfile.gettempdir()) / "untitled"
        beside = False
    return base.with_name(f"{base.name}{BACKUP_INFIX}{stamp}.max"), beside


def backup_path_for(scene: str | Path | None, stamp: str | None = None) -> Path:
    """<scene without .max>.plate-backup.<ts>.max next to the scene, or untitled.plate-backup.<ts>.max
    in the temp folder for a scene that was never saved. When the natural name does not fit the file
    system (recipes.fits_filesystem) the backup is plate-<scene hash>.plate-backup.<ts>.max in the temp
    folder instead."""
    stamp = stamp or _stamp()
    natural, beside = _natural_backup_path(scene, stamp)
    if not beside or fits_filesystem(natural):
        return natural
    return Path(tempfile.gettempdir()) / f"plate-{scene_key(str(scene))}{BACKUP_INFIX}{stamp}.max"


def backup_note(scene: str | Path | None, dest: str | Path) -> str | None:
    """A line for the log when the backup for scene is not beside it because the scene's name is too
    long for one (None otherwise); dest is the path backup() returned."""
    natural, beside = _natural_backup_path(scene, _stamp())
    if not beside or fits_filesystem(natural):
        return None
    return (
        f"backup stored in {Path(str(dest)).parent} as {Path(str(dest)).name} "
        "(the scene name is too long for one beside it)"
    )


def backup() -> Path:
    """Save a backup copy of the open scene and return its path. Raises BackupFailed."""
    rt = runtime()
    first = dest = backup_path_for(scene_path())
    n = 2
    while dest.exists():  # two backups in one second: never overwrite the first
        dest = first.with_name(f"{first.stem}-{n}{first.suffix}")
        n += 1
    try:
        # pymxs maps Python kwargs to MAXScript keyword arguments (MaxDirector's backup.py note)
        ok = rt.saveMaxFile(str(dest), useNewFile=False, quiet=True, clearNeedSaveFlag=False)
    except Exception as exc:
        raise BackupFailed(f"backup to {dest} raised {type(exc).__name__}: {exc}") from exc
    if not ok or not dest.is_file():
        raise BackupFailed(f"backup failed to {dest}: nothing was changed")
    return dest


@contextmanager
def one_undo(label: str = "PLATE") -> Iterator[None]:
    """One named undo record for everything inside the block (TODO-verify theHold on Max 2026)."""
    hold = rt_attr("theHold")
    if hold is None:
        raise RuntimeError("theHold is not available: refusing to change the scene without an undo record")
    hold.Begin()
    try:
        yield
    except BaseException:
        try:
            hold.Cancel()  # exactly this hold, LIFO-legal
        except Exception:
            pass
        raise
    else:
        try:
            hold.Accept(label)
        except Exception:
            try:
                hold.Cancel()  # never leave a hold open
            except Exception:
                pass
            raise


def journal_dir() -> Path:
    return app_data_dir() / "logs" / "journal"


class Journal:
    """A JSON breadcrumb of one operation: scene, backup, every step, the outcome. Written after each
    record so it survives a crash; a write failure never stops the operation it describes."""

    def __init__(self, scene: str | Path | None, backup_path: str | Path | None, op: str = "send"):
        stamp = _stamp()
        folder = journal_dir()
        self.path = folder / f"{op}-{stamp}-{os.getpid()}.json"
        n = 2
        while self.path.exists():
            self.path = folder / f"{op}-{stamp}-{os.getpid()}-{n}.json"
            n += 1
        self.data: dict[str, Any] = {
            "op": op,
            "scene": str(scene) if scene else None,
            "backup": str(backup_path) if backup_path else None,
            "started": stamp,
            "steps": [],
            "finished": None,
        }
        self._flush()

    def record(self, step: str, target: str = "", status: str = "ok", detail: str = "") -> None:
        self.data["steps"].append({"step": step, "target": target, "status": status, "detail": detail})
        self._flush()

    def finish(self, status: str) -> None:
        self.data["finished"] = status
        self.data["ended"] = _stamp()
        self._flush()

    def _flush(self) -> None:
        try:
            atomic_write_json(self.path, self.data)
        except OSError:
            pass
