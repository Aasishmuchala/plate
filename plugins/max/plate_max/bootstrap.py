"""Launch entry point. The startup script registers the PLATE_Dock macro (register_macro) and the
macro calls launch(); nothing here imports Qt at module level, so plate_max imports headless.

launch() guards double launch: a second click while the dock is opening, or after it is open, raises
the existing dock instead of building another one. A failure to open (no PySide6, a Qt error) is
shown in a message box and the Listener, never raised into Max. There is no auto-update here
(BUILD_PLAN decision 25): updating the plugin is a git pull by hand on a quiet day.
"""

from __future__ import annotations

from typing import Any

from .rt import runtime

MACRO_CATEGORY = "PLATE"
MACRO_NAME = "PLATE_Dock"
MACRO_TOOLTIP = "Open the PLATE dock"
MACRO_BUTTON_TEXT = "PLATE Dock"
# MAXScript the macro runs: the startup script already put <repo>/plugins/max on sys.path.
MACRO_SCRIPT = 'python.Execute "import plate_max.bootstrap as _plate; _plate.launch()"'

_state: dict[str, Any] = {"dock": None, "launching": False}


def register_macro() -> str:
    """Register the PLATE_Dock macroscript (Customize > Customize User Interface > PLATE category).
    Safe to call again: macros.new with the same name replaces the earlier one. Returns the macro name."""
    runtime().macros.new(MACRO_CATEGORY, MACRO_NAME, MACRO_TOOLTIP, MACRO_BUTTON_TEXT, MACRO_SCRIPT)
    return MACRO_NAME


def _report(message: str) -> None:
    try:
        runtime().messageBox(message, title="PLATE")
    except Exception:  # noqa: BLE001 off-Max, or pymxs itself is unusable
        pass
    print("[PLATE] " + message)


def launch(controller: Any = None) -> Any:
    """Open (or raise) the dock. Returns the dock widget, or None when it could not open."""
    if _state["launching"]:
        return _state["dock"]
    _state["launching"] = True
    try:
        from .ui import dock as dock_module  # imported here: Qt exists only inside Max (and the tests)

        _state["dock"] = dock_module.show_dock(controller)
        return _state["dock"]
    except Exception as exc:  # noqa: BLE001 surface a readable message inside Max
        _report(f"PLATE failed to launch:\n{type(exc).__name__}: {exc}")
        return None
    finally:
        _state["launching"] = False


def reset() -> None:
    """Forget the launched dock (tests; the dock module keeps its own singleton)."""
    _state["dock"] = None
    _state["launching"] = False
