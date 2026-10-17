# Shared Max bridge: one dock, one set of callbacks, one Qt

PLATE, MaxDirector and MaxGaffer all run inside the same 3ds Max 2026 session. Without a shared bridge,
each one opens its own floating dock, keeps its own `show_dock()` singleton, and is free to call
`callbacks.addScript` itself. This is what goes wrong:

- **Three docks** fight over the same screen edge. `QMainWindow.saveState()` keys docks by objectName,
  so a twin name makes Max restore the wrong one.
- **Duplicate callbacks.** Every re-launch or module reload can stack another `#filePostOpen` script.
  One broken handler then raises a MAXScript error dialog in the middle of a render.
- **Two Qt bindings.** MaxDirector's and MaxGaffer's bootstraps put
  `%APPDATA%\Python\Python311\site-packages` at the front of `sys.path`. A stray `pip install PySide6`
  there replaces the PySide6 that Max ships against its own Qt DLLs, and a PySide2 import next to
  PySide6 crashes Max.

`plate_max/shared/` fixes all three. Standard library only, Python 3.11, no Qt and no pymxs at import.

| module | what it gives you |
|---|---|
| `host.py` | `DockHost`: ONE `QDockWidget` (objectName `PlateSharedDock`) with a tab per panel. `register_panel(name, factory, title=, on=)`, `launch(parent=, select=)`, `remove_panel`, `close`, `unload`. Session API: `get_host()`, `launch_panel(...)` (the adapter entry point), `launch_plate(controller)`, `plate_panel(controller)`. |
| `mux.py` | `CallbackMux`: one Max script per event for `#filePostOpen`, `#preRender`, `#postRender`, `#viewportChange`, all under id `#PLATE_shared_bridge`, each fanning out to Python subscribers. Also `subscribe(event, fn, owner)`, `unsubscribe_owner`, `dispatch`, `unload`, `get_mux()`, and `once_per_session(key, fn)`. |
| `qt_guard.py` | The PySide6 collision guard. `check_bindings()` (two bindings is fatal; a binding from outside the host Python, or a split shiboken, gets a warning), `preflight()` (a `sys.path` entry that would shadow Max's PySide6), `standalone_open()` (a tool's own dock is still open), `find_dock()`. |

## Rules the bridge keeps

- **One Max script per event.** Install removes our id on that event before it adds the script. A
  re-launch, a reload or a second plugin can never stack duplicates. Scripts install lazily: an event
  with no subscribers has no script.
- **Never persistent.** Every script uses `persistent:false`, so nothing is saved into a client `.max`.
  Safe Scene Script Execution flags embedded scripts, and custody says the scene carries nothing of ours.
- **Handlers never reach Max.** A handler that raises is logged (`[PLATE shared] ...`, and
  `get_mux().errors`), and the others still run. A handler whose widget is deleted ("already deleted")
  is dropped. An event re-fired inside its own dispatch does not recurse. The MAXScript itself is
  `try (python.Execute "...") catch (format ...)`, so if plate_max disappears from `sys.path` you get
  one Listener line, not an error dialog.
- **Double launch is a no-op.** The second `launch()` or `launch_panel()` raises the same dock and
  tab. No factory runs twice, no tab or subscription is added twice.
- **A broken tool cannot take the dock down.** A factory that raises gets a placeholder tab, and the
  next launch retries only that tab.
- **No twin UIs.** While a tool's standalone dock is visible (`PlateDock`, `MaxGafferDock`,
  `MaxDirectorDock`), its tab is a placeholder that says why. A module reload that leaves
  `PlateSharedDock` in Max's main window gets that dock adopted, not a second one.
- **Qt stays behind PLATE's Qt boundary.** `tests/test_stdlib_only.py` allows PySide6 and qtmax only
  in `plate_max/ui/`. `DockHost.launch()` imports `plate_max.ui.dock` inside the call (as
  `bootstrap.launch()` does) and reuses its QtCore/QtWidgets, so the host is never the module that
  pulls a second binding into Max.
- **Main thread only.** Max fires callbacks there, and pymxs and Qt need it. `#preRender` and
  `#postRender` fire for every render, including each of MaxGaffer's loop renders, so keep those
  handlers cheap.
- **A session is the Max process.** `once_per_session` guards outlive `unload()`. A guarded call that
  raises does not use up the guard.

## PLATE adoption (follow-up in T20's files; not done here)

1. In `plate_max/ui/dock.py`, add the hook that `host._max_main_window()` already looks for:

   ```python
   def max_main_window():
       try:
           import qtmax

           return qtmax.GetQMaxMainWindow()
       except Exception:  # noqa: BLE001
           return None
   ```

2. In `plate_max/bootstrap.py`, keep the double-launch guard and replace the body of `launch()` with
   `from . import shared; _state["dock"] = shared.launch_plate(controller)`. `PLATE_ON` refreshes the
   camera list on `#filePostOpen`.

Since T77, PLATE opens as a tab of the shared dock by default; `PLATE_STANDALONE_DOCK=1` (or `show_dock(standalone=True)`) still opens the old floating dock until the box check below passes. A second macro (`PLATE_SharedDock`)
can run `python.Execute "import plate_max.shared as _s; _s.launch_plate()"` for side-by-side testing.

## MaxGaffer adapter (about 20 lines, in its repo; not implemented here)

This replaces `launch()` in `maxgaffer/bootstrap.py`. If PLATE is not installed, MaxGaffer opens its
own dock exactly as it does today.

```python
def launch():
    _ensure_usersite_on_path()
    try:
        from plate_max import shared  # on sys.path once PLATE's startup script ran
    except ImportError:
        shared = None
    try:
        from .ui import dock as mg

        if shared is None:
            return mg.show_dock()  # no PLATE: the standalone dock, unchanged
        import qtmax

        return shared.launch_panel(
            "maxgaffer",
            mg.MaxGafferDock,  # the class is the factory: MaxGafferDock() builds the tab page
            on={"filePostOpen": lambda w: (w.refresh_cameras(), w.rebuild_rig_controls())},
            parent=qtmax.GetQMaxMainWindow(),
        )
    except Exception as e:  # noqa: BLE001
        ...  # the existing "MaxGaffer failed to launch" message box, unchanged
        return None
```

Two more changes on the MaxGaffer side:

- In `_ensure_usersite_on_path`, append the user site to `sys.path` instead of inserting it at index 0,
  or call `shared.preflight()` first and stop on `PYSIDE6_SHADOWING_RISK`.
- Any session-once setup goes through `shared.once_per_session("maxgaffer.<what>", fn)`, for example
  creating the exposure control.

## MaxDirector adapter (about 20 lines, in its repo; not implemented here)

```python
def launch():
    missing = _missing()
    if missing:
        ...  # the existing dependency message box, unchanged
        return None
    try:
        from plate_max import shared
    except ImportError:
        shared = None
    from .ui import dock as md

    if shared is None:
        return md.show_dock()  # no PLATE: the standalone window, unchanged
    import qtmax

    return shared.launch_panel(
        "maxdirector",
        md.MaxDirectorDock,
        on={"filePostOpen": lambda w: w._refresh_buttons()},  # no public scene hook yet
        parent=qtmax.GetQMaxMainWindow(),
    )
```

MaxDirector's `show_dock()` creates a `QApplication` when there is none. The host never does: inside
Max one always exists, and the tests make their own.

## For any tool that adopts the bridge

- Do not call `callbacks.addScript` for the four shared events. Subscribe through `on=` (bound to the
  panel and removed with it) or `get_mux().subscribe(event, fn, owner="<tool>")`. Other events need
  their own id and `persistent:false`.
- Do not open the standalone dock as well. If you do, the shared tab stays a placeholder until that
  window closes.
- A panel is a zero-argument factory that returns a `QWidget`. The host reparents it into the tab
  widget. Panel names are lowercase. `plate`, `maxdirector` and `maxgaffer` come first in that order,
  and any other name follows in registration order.

## What is proven here and what is not

Proven on this Mac by `plugins/max/tests/test_shared_bridge.py`, using the fake runtime plus an offscreen
PySide6 6.12 under a stand-in `QMainWindow`:

- two panels register in one tabbed dock, and a panel registered after launch joins it in order;
- a double launch (direct or through `launch_panel`) is a no-op;
- one Max script per event under our id, with `persistent:false`. The fake really runs the registered
  MAXScript's `python.Execute` code, so the path from Max event to every subscriber is exercised;
- a failing handler does not stop the others, dead-widget handlers are dropped, and there is no
  recursion;
- `unload()` removes every shared script and subscriber and leaves other plugins' callbacks alone;
- `once_per_session` runs once and lets a failure retry;
- the binding guard (fake module maps), placeholder tabs for a broken factory and an open standalone
  dock, adoption after a reload, refusal on two bindings, and the parent fallbacks.

verify-on-box (none of this can be measured on the Mac):

1. PLATE and MaxGaffer docks open together, one `PlateSharedDock` with two tabs, with no PySide6 or
   callback collision across a file open and a render.
2. `callbacks.show id:#PLATE_shared_bridge` lists exactly the subscribed events after launch, and
   nothing after `plate_max.shared.unload()`.
3. The four event names fire on Max 2026, and `#viewportChange` does not fire often enough to matter.
4. pymxs maps `id=` and `persistent=` to the MAXScript keywords.
5. With plate_max removed from `sys.path`, a file open prints one Listener line and shows no dialog.
6. `QWidget.find(windows.getMAXHWND())` returns the main window (the fallback when no `parent` and no
   `max_main_window()`).
7. Inside Max, `PySide6.__file__` lies under `sys.prefix` or the Max folder. If not, the guard
   false-warns `QT_BINDING_OUTSIDE_HOST`.
8. Max restores the shared dock's position across a restart without disturbing the `PlateDock` and
   `MaxGafferDock` layout entries.
9. A scene saved and reopened with Safe Scene Script Execution on carries no PLATE script.
