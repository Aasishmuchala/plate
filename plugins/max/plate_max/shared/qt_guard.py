"""PySide6 collision guard for the three docks that share one 3ds Max process.

What collides when PLATE, MaxDirector and MaxGaffer all live in Max's Python:

  * two Qt bindings: PySide2 and PySide6 loaded side by side wrap two Qt builds in one process, which
    crashes Max on the first widget that crosses over. Fatal: the shared host refuses to build.
  * a shadowing PySide6: MaxDirector's and MaxGaffer's bootstraps put %APPDATA%\\Python\\Python311\\
    site-packages at the FRONT of sys.path, so a stray `pip install --target ... PySide6` there wins over
    the build Max ships against its own Qt DLLs. Reported (once per session), not fatal: if it imported,
    it may work, and the artist needs to know why Max later dies.
  * a shiboken6 from a different folder than PySide6 (half of a second install). Reported.
  * two live UIs for one tool: a plugin's standalone dock (PlateDock, MaxGafferDock, MaxDirectorDock)
    still open while the shared host builds the same tool as a tab means two sets of timers, two
    controllers and doubled callbacks. The host shows a placeholder tab instead (host.py).
  * two docks under one objectName: QMainWindow.saveState/restoreState keys docks by objectName, so the
    host uses its own (PlateSharedDock) and adopts an existing one instead of adding a twin (a module
    reload keeps the old QDockWidget alive in Max's main window).

The binding checks read sys.modules and file paths only, so they run with no Qt at all. The widget
checks take the QtWidgets module as an argument: Qt is imported only inside plate_max/ui
(tests/test_stdlib_only.py), never here. Standard library only.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BINDINGS = ("PySide6", "PySide2")
SHIBOKEN = {"PySide6": "shiboken6", "PySide2": "shiboken2"}
# Standalone windows of each tool, matched on objectName or class name (the plugins' own show_dock():
# PLATE and MaxGaffer name their QDockWidget, MaxDirector opens a bare top-level MaxDirectorDock).
STANDALONE_NAMES = {
    "plate": ("PlateDock",),
    "maxgaffer": ("MaxGafferDock",),
    "maxdirector": ("MaxDirectorDock",),
}

TWO_BINDINGS = "TWO_QT_BINDINGS"
OUTSIDE_HOST = "QT_BINDING_OUTSIDE_HOST"
SHIBOKEN_SPLIT = "SHIBOKEN_SPLIT"
SHADOWING_RISK = "PYSIDE6_SHADOWING_RISK"
STANDALONE_OPEN = "STANDALONE_DOCK_OPEN"


@dataclass(frozen=True)
class Collision:
    code: str
    detail: str
    fatal: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def host_roots() -> list[str]:
    """Folders a binding Max (or this venv) ships may live under: the interpreter's prefixes and the
    folder of the executable (3dsmax.exe inside Max)."""
    roots = [sys.prefix, sys.base_prefix, sys.exec_prefix]
    if sys.executable:
        roots.append(os.path.dirname(sys.executable))
    return _unique(roots)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _unique(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if p and _norm(p) not in {_norm(o) for o in out}:
            out.append(p)
    return out


def _under(path: str, root: str) -> bool:
    try:
        p, r = _norm(path), _norm(root)
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def _origin(module: Any) -> str:
    """The package folder a module was imported from ('' when it has no file)."""
    path = getattr(module, "__file__", None) or ""
    return os.path.dirname(path) if path else ""


def loaded_bindings(modules: Mapping[str, Any] | None = None) -> dict[str, str]:
    """{binding: package folder} for every Qt binding imported in this process (sys.modules entries
    set to None, the blocked-import marker, do not count)."""
    mods = sys.modules if modules is None else modules
    return {name: _origin(mods[name]) for name in BINDINGS if mods.get(name) is not None}


def check_bindings(
    modules: Mapping[str, Any] | None = None, roots: Iterable[str] | None = None
) -> list[Collision]:
    """The Qt-binding collisions in this process right now (empty is healthy)."""
    mods = sys.modules if modules is None else modules
    trusted = list(roots) if roots is not None else host_roots()
    loaded = loaded_bindings(mods)
    found: list[Collision] = []
    if len(loaded) > 1:
        found.append(
            Collision(
                TWO_BINDINGS,
                "both {} are imported in this Max session; mixing two Qt builds crashes Max. Restart "
                "Max and remove the plugin that imports PySide2".format(" and ".join(sorted(loaded))),
                fatal=True,
            )
        )
    for name, folder in loaded.items():
        if folder and trusted and not any(_under(folder, r) for r in trusted):
            found.append(
                Collision(
                    OUTSIDE_HOST,
                    f"{name} was imported from {folder}, not from the Python this host ships; a copy in "
                    "the user site-packages shadows Max's own build. Remove it there",
                )
            )
        shiboken = mods.get(SHIBOKEN[name])
        sfolder = _origin(shiboken) if shiboken is not None else ""
        if folder and sfolder and _norm(os.path.dirname(sfolder)) != _norm(os.path.dirname(folder)):
            found.append(
                Collision(
                    SHIBOKEN_SPLIT,
                    f"{name} comes from {folder} but {SHIBOKEN[name]} from {sfolder}; one of them is half "
                    "of a second install",
                )
            )
    return found


def shadowing_entries(path: Iterable[str] | None = None, roots: Iterable[str] | None = None) -> list[str]:
    """sys.path entries outside the host's own folders that hold a PySide6 package: whichever is
    first on sys.path wins the next `import PySide6`. A preflight for before anything imports Qt."""
    entries = list(sys.path if path is None else path)
    trusted = list(roots) if roots is not None else host_roots()
    out = []
    for entry in entries:
        if not entry or any(_under(entry, r) for r in trusted):
            continue
        if os.path.isdir(os.path.join(entry, "PySide6")):
            out.append(entry)
    return out


def preflight(path: Iterable[str] | None = None, roots: Iterable[str] | None = None) -> list[Collision]:
    """check_bindings() plus the shadowing risk on sys.path (only while PySide6 is not yet imported)."""
    found = check_bindings(roots=roots)
    if "PySide6" not in loaded_bindings():
        for entry in shadowing_entries(path, roots):
            found.append(
                Collision(
                    SHADOWING_RISK,
                    f"{entry} holds a PySide6 package and is on sys.path; if it is imported first it "
                    "replaces the build that matches Max's Qt",
                )
            )
    return found


# --------------------------------------------------------------------------- widget-tree checks


def _candidates(qtwidgets: Any, parent: Any) -> list[Any]:
    """Top-level windows plus the QDockWidgets under parent (Max's main window), each once."""
    seen: list[Any] = []
    try:
        pool = list(qtwidgets.QApplication.topLevelWidgets())
    except Exception:  # noqa: BLE001
        pool = []
    if parent is not None:
        try:
            pool += list(parent.findChildren(qtwidgets.QDockWidget))
        except Exception:  # noqa: BLE001
            pass
    for w in pool:
        if not any(w is s for s in seen):
            seen.append(w)
    return seen


def find_dock(qtwidgets: Any, parent: Any, object_name: str) -> Any:
    """The QDockWidget called object_name under parent (among the top-level windows when there is no
    parent), else None."""
    try:
        if parent is not None:
            pool = list(parent.findChildren(qtwidgets.QDockWidget))
        else:
            pool = list(qtwidgets.QApplication.topLevelWidgets())
    except Exception:  # noqa: BLE001
        return None
    for w in pool:
        try:
            if isinstance(w, qtwidgets.QDockWidget) and w.objectName() == object_name:
                return w
        except RuntimeError:  # deleted underneath us
            continue
    return None


def _names_of(qtwidgets: Any, widget: Any) -> set[str]:
    names = {widget.objectName(), type(widget).__name__}
    if isinstance(widget, qtwidgets.QDockWidget) and widget.widget() is not None:
        inner = widget.widget()
        names |= {inner.objectName(), type(inner).__name__}
    return names - {""}


def standalone_open(qtwidgets: Any, parent: Any, exclude: Any = None) -> dict[str, str]:
    """{panel: window name} for every tool whose own standalone dock is visible right now. exclude is
    the shared dock itself (its tab pages are never top-level, so they never match)."""
    open_now: dict[str, str] = {}
    for w in _candidates(qtwidgets, parent):
        if exclude is not None and w is exclude:
            continue
        try:
            if not w.isVisible():
                continue
            names = _names_of(qtwidgets, w)
        except RuntimeError:
            continue
        for panel, known in STANDALONE_NAMES.items():
            hit = next((n for n in known if n in names), None)
            if hit and panel not in open_now:
                open_now[panel] = hit
    return open_now
