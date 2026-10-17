"""The shared Max bridge: one dock, one set of Max callbacks, one Qt binding for PLATE, MaxDirector
and MaxGaffer when they run in the same 3ds Max session (plugins/max/SHARED_BRIDGE.md).

  host.py      DockHost: ONE QDockWidget with a tab per registered panel; launch_panel() for adapters
  mux.py       CallbackMux: one #filePostOpen/#preRender/#postRender/#viewportChange script each,
               fanned out to Python subscribers; once_per_session guards; unload()
  qt_guard.py  the PySide6 collision guard (two bindings, a shadowing user-site copy, twin docks)

Importing this package pulls in no Qt and no pymxs: Qt is borrowed from plate_max/ui inside
DockHost.launch(), and pymxs is reached only through plate_max.rt.
"""

from __future__ import annotations

from .host import (
    DOCK_OBJECT_NAME,
    KNOWN_PANELS,
    DockHost,
    HostError,
    get_host,
    launch,
    launch_panel,
    launch_plate,
    plate_panel,
    register_panel,
    unload,
)
from .mux import CALLBACK_ID, EVENTS, CallbackMux, dispatch, get_mux, once_per_session
from .qt_guard import Collision, check_bindings, preflight

__all__ = [
    "CALLBACK_ID",
    "DOCK_OBJECT_NAME",
    "EVENTS",
    "KNOWN_PANELS",
    "CallbackMux",
    "Collision",
    "DockHost",
    "HostError",
    "check_bindings",
    "dispatch",
    "get_host",
    "launch",
    "launch_panel",
    "launch_plate",
    "get_mux",
    "once_per_session",
    "plate_panel",
    "preflight",
    "register_panel",
    "unload",
]
