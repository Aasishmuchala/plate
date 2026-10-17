"""The shared Max bridge (plate_max/shared) on the fake runtime and an offscreen Qt.

Two halves. The CallbackMux half needs no Qt and always runs: a FakeCallbacks stands in for
rt.callbacks and really executes the MAXScript the mux registers (it pulls the Python out of
`python.Execute "..."` and runs it), so the fan-out is proven end to end from a Max event to every
subscriber. The DockHost half builds real PySide6 widgets with QT_QPA_PLATFORM=offscreen under a
QMainWindow standing in for Max's main window, and skips cleanly without the PySide6 wheel.
What this cannot prove: that Max 2026 fires these four events with these names, that pymxs maps the
id:/persistent: keywords, and how PLATE and MaxGaffer look docked together (SHARED_BRIDGE.md, verify-on-box).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before anything imports Qt

from fake_pymxs import fake_scene  # noqa: E402

import plate_max  # noqa: E402
from plate_max import rt, shared  # noqa: E402
from plate_max.shared import host as host_module  # noqa: E402
from plate_max.shared import mux as mux_module  # noqa: E402
from plate_max.shared import qt_guard  # noqa: E402
from plate_max.ui import controller as ctl  # noqa: E402

PLUGIN = Path(plate_max.__file__).resolve().parent.parent
OUR_ID = mux_module.CALLBACK_ID
PY_EXECUTE = re.compile(r'python\.Execute "((?:[^"\\]|\\.)*)"')


class FakeCallbacks:
    """rt.callbacks: addScript/removeScripts with MAXScript's keywords, and fire(event), which runs
    each registered script the way Max would: the Python inside python.Execute "..." is executed in a
    fresh __main__, and an exception lands in the script's own catch (recorded in .caught)."""

    def __init__(self):
        self.scripts: list[dict] = []
        self.caught: list[str] = []
        self.calls: list[tuple] = []

    def addScript(self, event, script, id=None, persistent=False):  # noqa: N802, A002 MAXScript names
        self.calls.append(("addScript", str(event), str(id), persistent))
        self.scripts.append(
            {"event": str(event), "script": str(script), "id": str(id), "persistent": persistent}
        )

    def removeScripts(self, event=None, id=None):  # noqa: N802, A002
        self.calls.append(("removeScripts", None if event is None else str(event), str(id)))
        self.scripts = [
            s
            for s in self.scripts
            if not ((event is None or s["event"] == str(event)) and (id is None or s["id"] == str(id)))
        ]

    def ours(self, event=None):
        return [s for s in self.scripts if s["id"] == OUR_ID and (event is None or s["event"] == event)]

    def fire(self, event):
        for entry in [s for s in self.scripts if s["event"] == event]:
            match = PY_EXECUTE.search(entry["script"])
            if match is None:
                continue  # a foreign MAXScript-only callback
            code = match.group(1).replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
            try:
                exec(compile(code, "<maxscript python.Execute>", "exec"), {"__name__": "__main__"})
            except Exception as exc:  # the MAXScript try/catch around python.Execute
                self.caught.append(f"{type(exc).__name__}: {exc}")


@pytest.fixture(autouse=True)
def _fresh_singletons():
    mux_module._reset()
    host_module._reset()
    yield
    current = host_module._state["host"]
    if current is not None:
        current.close()
    mux_module._reset()
    host_module._reset()


@pytest.fixture
def max_rt():
    """fake_scene() with FakeCallbacks as rt.callbacks, installed as the plate_max runtime."""
    runtime = fake_scene()
    runtime.callbacks = FakeCallbacks()
    rt.set_runtime(runtime)
    return runtime


# ===================================================================== CallbackMux (no Qt)
def test_two_owners_share_one_max_callback_per_event_and_both_hear_it(max_rt):
    heard = []
    m = shared.get_mux()
    m.subscribe("filePostOpen", lambda: heard.append("plate"), owner="plate")
    m.subscribe("filePostOpen", lambda: heard.append("maxgaffer"), owner="maxgaffer")
    m.subscribe("postRender", lambda: heard.append("gaffer-render"), owner="maxgaffer")

    cb = max_rt.callbacks
    assert [s["event"] for s in cb.ours()] == ["filePostOpen", "postRender"]  # lazy: nobody wants the rest
    assert all(s["persistent"] is False for s in cb.ours())  # never saved into a client .max
    assert m.installed == ("filePostOpen", "postRender")
    assert m.subscribers("filePostOpen") == ["plate", "maxgaffer"]

    cb.fire("filePostOpen")
    assert heard == ["plate", "maxgaffer"]
    cb.fire("postRender")
    cb.fire("preRender")  # no script, no handler
    assert heard == ["plate", "maxgaffer", "gaffer-render"]
    assert cb.caught == []


def test_resubscribing_reinstalling_or_a_reloaded_mux_never_stacks_duplicates(max_rt):
    cb = max_rt.callbacks
    cb.addScript("filePostOpen", "print 1", id="MaxGaffer_own")  # another plugin's own callback
    m = shared.get_mux()
    for i in range(5):
        m.subscribe("filePostOpen", lambda: None, owner=f"o{i}")
    assert m.install() and m.install()
    reloaded = mux_module.CallbackMux()  # what importlib.reload would leave behind
    reloaded.subscribe("filePostOpen", lambda: None, owner="again")
    assert len(cb.ours("filePostOpen")) == 1
    assert [s["id"] for s in cb.scripts if s["event"] == "filePostOpen"].count("MaxGaffer_own") == 1


def test_a_failing_handler_is_logged_and_the_others_still_run(max_rt, capsys):
    ran = []

    def broken():
        raise ValueError("MaxGaffer lost its session file")

    m = shared.get_mux()
    m.subscribe("filePostOpen", broken, owner="maxgaffer")
    m.subscribe("filePostOpen", lambda: ran.append("plate"), owner="plate")
    max_rt.callbacks.fire("filePostOpen")
    assert ran == ["plate"]
    assert max_rt.callbacks.caught == []  # nothing escaped into MAXScript
    assert "maxgaffer filePostOpen handler failed: ValueError" in m.errors[-1]
    assert "[PLATE shared]" in capsys.readouterr().out
    assert m.subscribers("filePostOpen") == ["maxgaffer", "plate"]  # a plain error keeps the handler


def test_a_handler_bound_to_a_deleted_widget_is_dropped():
    m = mux_module.CallbackMux()

    def dead():
        raise RuntimeError("Internal C++ object (PySide6.QtWidgets.QLabel) already deleted.")

    m.subscribe("viewportChange", dead, owner="maxdirector")
    assert m.dispatch("viewportChange") == 0
    assert m.subscribers("viewportChange") == []


def test_an_event_fired_inside_its_own_dispatch_does_not_recurse():
    m = mux_module.CallbackMux()
    depth = []

    def opens_another_file():
        depth.append(len(depth))
        m.dispatch("filePostOpen")  # a handler that merges or opens a file re-fires the event

    m.subscribe("filePostOpen", opens_another_file, owner="maxdirector")
    assert m.dispatch("filePostOpen") == 1
    assert depth == [0]


def test_unsubscribing_one_handler_or_a_whole_owner():
    m = mux_module.CallbackMux()
    ran = []
    cancel = m.subscribe("preRender", lambda: ran.append("a"), owner="plate")
    m.subscribe("preRender", lambda: ran.append("b"), owner="maxgaffer")
    m.subscribe("postRender", lambda: ran.append("c"), owner="maxgaffer")
    cancel()
    m.dispatch("preRender")
    assert ran == ["b"]
    assert m.unsubscribe_owner("maxgaffer") == 2
    m.dispatch("preRender")
    m.dispatch("postRender")
    assert ran == ["b"]


def test_a_handler_may_unsubscribe_itself_mid_dispatch():
    m = mux_module.CallbackMux()
    ran = []
    holder = {}

    def once():
        ran.append("once")
        holder["cancel"]()

    holder["cancel"] = m.subscribe("postRender", once, owner="plate")
    m.subscribe("postRender", lambda: ran.append("other"), owner="maxgaffer")
    m.dispatch("postRender")
    m.dispatch("postRender")
    assert ran == ["once", "other", "other"]


def test_unload_removes_every_shared_callback_and_subscriber_and_leaves_foreign_ones(max_rt):
    cb = max_rt.callbacks
    cb.addScript("postRender", "print 2", id="MaxDirector_own")
    ran = []
    m = shared.get_mux()
    for event in mux_module.EVENTS:
        m.subscribe(event, lambda e=event: ran.append(e), owner="plate")
    assert len(cb.ours()) == 4

    assert m.unload() is True
    assert cb.ours() == []
    assert [s["id"] for s in cb.scripts] == ["MaxDirector_own"]
    assert m.installed == ()
    assert all(m.subscribers(e) == [] for e in mux_module.EVENTS)
    cb.fire("filePostOpen")
    assert ran == []
    assert m.unload() is True  # twice is fine

    m.subscribe("filePostOpen", lambda: ran.append("back"), owner="plate")  # a re-launch reinstalls
    cb.fire("filePostOpen")
    assert ran == ["back"] and len(cb.ours("filePostOpen")) == 1


def test_off_max_the_subscribers_still_run_on_a_direct_dispatch():
    rt.set_runtime(None)  # no pymxs here: runtime() raises
    m = shared.get_mux()
    ran = []
    m.subscribe("filePostOpen", lambda: ran.append(1), owner="plate")
    assert m.installed == ()
    assert m.install() is False
    assert "filePostOpen" in m.last_install_error
    assert mux_module.dispatch("filePostOpen") == 1
    assert ran == [1]
    assert m.unload() is True  # nothing was installed, so nothing is left behind


def test_the_registered_script_is_maxscript_that_reaches_the_shared_mux(max_rt):
    script = mux_module.callback_script("preRender")
    assert script.startswith('try (python.Execute "') and "catch (format" in script
    code = PY_EXECUTE.search(script).group(1)
    assert '"' not in code and "\n" not in code
    assert "plate_max.shared.mux" in code and "dispatch('preRender')" in code
    # the script imports plate_max.shared.mux by attribute: the package must not shadow its submodules
    assert isinstance(shared.mux, types.ModuleType) and isinstance(shared.host, types.ModuleType)
    hits = []
    shared.get_mux().subscribe("preRender", lambda: hits.append(1), owner="plate")
    max_rt.callbacks.fire("preRender")
    assert hits == [1]


def test_a_broken_install_prints_one_listener_line_instead_of_a_maxscript_error(max_rt, monkeypatch):
    shared.get_mux().subscribe("filePostOpen", lambda: None, owner="plate")
    monkeypatch.setitem(sys.modules, "plate_max.shared.mux", None)  # plate_max gone from sys.path
    max_rt.callbacks.fire("filePostOpen")
    assert len(max_rt.callbacks.caught) == 1


def test_unknown_events_and_non_callables_are_rejected():
    m = mux_module.CallbackMux()
    with pytest.raises(ValueError, match="unknown event"):
        m.subscribe("filePreSave", lambda: None)
    with pytest.raises(TypeError):
        m.subscribe("filePostOpen", "refresh")
    with pytest.raises(ValueError):
        mux_module.callback_script("systemPostReset")
    with pytest.raises(ValueError):
        mux_module.CallbackMux(events=("filePostOpen", "selectionSetChanged"))
    assert m.dispatch("selectionSetChanged") == 0


def test_once_per_session_runs_once_retries_a_failure_and_outlives_unload(max_rt):
    calls = []

    def seed():
        calls.append(1)
        return "seeded"

    assert shared.once_per_session("maxgaffer.seed_dome", seed) == "seeded"
    assert shared.once_per_session("maxgaffer.seed_dome", seed) == "seeded"
    assert calls == [1]

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("daemon not up yet")
        return "ok"

    with pytest.raises(OSError):
        shared.once_per_session("plate.banner", flaky)
    assert not mux_module.ran_this_session("plate.banner")
    assert shared.once_per_session("plate.banner", flaky) == "ok"
    assert len(attempts) == 2

    @shared.once_per_session("plate.macro")
    def register():
        calls.append("macro")
        return 7

    assert register() == 7 and register() == 7
    assert calls == [1, "macro"]
    shared.get_mux().unload()
    assert mux_module.ran_this_session("maxgaffer.seed_dome")  # a session is the Max process


def test_importing_the_shared_bridge_pulls_in_no_qt_and_no_pymxs():
    code = (
        "import sys; sys.modules['PySide6'] = None; sys.modules['qtmax'] = None; sys.modules['pymxs'] = None; "
        "import plate_max.shared as s; m = s.get_mux(); m.subscribe('filePostOpen', lambda: None, owner='x'); "
        "assert m.installed == (); assert s.dispatch('filePostOpen') == 1; "
        "assert not [k for k, v in sys.modules.items() if v is not None and k.split('.')[0] in "
        "('PySide6', 'PySide2', 'qtmax', 'pymxs', 'shiboken6')]"
    )
    env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


# ===================================================================== collision guard (no Qt)
def _module(path: Path) -> types.ModuleType:
    mod = types.ModuleType(path.parent.name)
    mod.__file__ = str(path)
    return mod


def test_the_binding_check_is_quiet_for_one_binding_from_the_host(tmp_path):
    site = tmp_path / "Max" / "Python" / "Lib" / "site-packages"
    mods = {
        "PySide6": _module(site / "PySide6" / "__init__.py"),
        "shiboken6": _module(site / "shiboken6" / "__init__.py"),
        "PySide2": None,  # a blocked import does not count
    }
    assert qt_guard.check_bindings(mods, roots=[str(tmp_path / "Max")]) == []
    assert qt_guard.loaded_bindings(mods) == {"PySide6": str(site / "PySide6")}


def test_the_binding_check_flags_two_bindings_a_user_site_copy_and_a_split_shiboken(tmp_path):
    host_site = tmp_path / "Max" / "Python" / "Lib" / "site-packages"
    user_site = tmp_path / "AppData" / "Roaming" / "Python" / "Python311" / "site-packages"
    mods = {
        "PySide6": _module(user_site / "PySide6" / "__init__.py"),
        "shiboken6": _module(host_site / "shiboken6" / "__init__.py"),
        "PySide2": _module(host_site / "PySide2" / "__init__.py"),
    }
    found = qt_guard.check_bindings(mods, roots=[str(tmp_path / "Max")])
    codes = [c.code for c in found]
    assert codes == [qt_guard.TWO_BINDINGS, qt_guard.OUTSIDE_HOST, qt_guard.SHIBOKEN_SPLIT]
    assert [c.fatal for c in found] == [True, False, False]
    assert str(user_site / "PySide6") in found[1].detail


def test_the_preflight_names_a_sys_path_entry_that_would_shadow_maxs_pyside6(tmp_path):
    user_site = tmp_path / "user-site"
    (user_site / "PySide6").mkdir(parents=True)
    clean = tmp_path / "clean"
    clean.mkdir()
    host_site = tmp_path / "Max" / "site-packages"
    (host_site / "PySide6").mkdir(parents=True)
    path = [str(user_site), str(clean), str(host_site), ""]
    assert qt_guard.shadowing_entries(path, roots=[str(tmp_path / "Max")]) == [str(user_site)]


def test_this_venvs_real_pyside6_passes_the_guard():
    pytest.importorskip("PySide6.QtWidgets")
    assert [c for c in qt_guard.check_bindings() if c.fatal] == []
    assert "PySide6" in qt_guard.loaded_bindings()


# ===================================================================== DockHost (Qt, offscreen)
@pytest.fixture(scope="module")
def qt():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return types.SimpleNamespace(app=app, QtCore=QtCore, QtWidgets=QtWidgets)


@pytest.fixture
def main_window(qt):
    """A QMainWindow standing in for qtmax.GetQMaxMainWindow()."""
    win = qt.QtWidgets.QMainWindow()
    win.setObjectName("FakeMaxMainWindow")
    win.resize(1600, 900)
    win.show()
    yield win
    win.close()
    win.deleteLater()
    _flush(qt)


def _flush(qt):
    qt.app.processEvents()
    qt.app.sendPostedEvents(None, qt.QtCore.QEvent.DeferredDelete)
    qt.app.processEvents()


@pytest.fixture
def panel_cls(qt):
    """A tool panel that records what the shared callbacks told it."""

    class ToolPanel(qt.QtWidgets.QLabel):
        def __init__(self, text):
            super().__init__(text)
            self.heard = []

    return ToolPanel


def _factory(panel_cls, name, counter):
    def build():
        counter[name] = counter.get(name, 0) + 1
        return panel_cls(name)

    return build


def _shared_docks(qt, main_window):
    return [
        d
        for d in main_window.findChildren(qt.QtWidgets.QDockWidget)
        if d.objectName() == shared.DOCK_OBJECT_NAME
    ]


def test_two_panels_register_into_one_tabbed_dock(qt, main_window, panel_cls):
    from plate_max.ui import dock as plate_dock

    made = {}
    h = shared.DockHost()
    assert h.register_panel("maxgaffer", _factory(panel_cls, "maxgaffer", made)) is True
    assert h.register_panel("plate", shared.plate_panel(ctl.FakeController())) is True
    dock = h.launch(parent=main_window)

    assert isinstance(dock, qt.QtWidgets.QDockWidget)
    assert _shared_docks(qt, main_window) == [dock]
    assert isinstance(dock.widget(), qt.QtWidgets.QTabWidget)
    assert h.tab_titles() == ["PLATE", "MaxGaffer"]  # known order, not registration order
    assert h.panels() == ["plate", "maxgaffer"]
    assert isinstance(h.widget("plate"), plate_dock.PlateDock)
    assert isinstance(h.widget("maxgaffer"), panel_cls)
    assert dock.isFloating() and dock.isVisible()
    assert made == {"maxgaffer": 1}
    h.close()


def test_double_launch_is_a_no_op(qt, main_window, panel_cls):
    made = {}
    h = shared.DockHost()
    h.register_panel("plate", _factory(panel_cls, "plate", made))
    h.register_panel("maxgaffer", _factory(panel_cls, "maxgaffer", made))
    first = h.launch(parent=main_window)
    pages = [h.widget("plate"), h.widget("maxgaffer")]
    second = h.launch(parent=main_window)
    assert second is first
    assert [h.widget("plate"), h.widget("maxgaffer")] == pages
    assert made == {"plate": 1, "maxgaffer": 1}
    assert h.tab_titles() == ["PLATE", "MaxGaffer"]
    assert len(_shared_docks(qt, main_window)) == 1
    h.close()


def test_double_launch_through_the_adapter_entry_point_is_a_no_op(qt, main_window, panel_cls, max_rt):
    made = {}
    on = {"filePostOpen": lambda w: w.heard.append("open")}
    for _ in range(2):  # the artist clicks the macro twice
        dock = shared.launch_panel(
            "maxgaffer", _factory(panel_cls, "maxgaffer", made), on=on, parent=main_window
        )
    assert dock is shared.get_host().dock
    assert made == {"maxgaffer": 1}
    assert shared.get_mux().subscribers("filePostOpen") == ["maxgaffer"]
    assert len(max_rt.callbacks.ours("filePostOpen")) == 1
    max_rt.callbacks.fire("filePostOpen")
    assert shared.get_host().widget("maxgaffer").heard == ["open"]


def test_a_panel_registered_after_launch_joins_the_open_dock_in_order(qt, main_window, panel_cls):
    made = {}
    h = shared.DockHost()
    h.register_panel("plate", _factory(panel_cls, "plate", made))
    dock = h.launch(parent=main_window)
    h.register_panel("maxgaffer", _factory(panel_cls, "maxgaffer", made))
    h.register_panel("staffage", _factory(panel_cls, "staffage", made), title="STAFFAGE")
    h.register_panel("maxdirector", _factory(panel_cls, "maxdirector", made))
    assert h.tab_titles() == ["PLATE", "MaxDirector", "MaxGaffer", "STAFFAGE"]
    assert h.launch(parent=main_window, select="maxgaffer") is dock
    assert dock.widget().currentWidget() is h.widget("maxgaffer")
    assert made == {"plate": 1, "maxgaffer": 1, "staffage": 1, "maxdirector": 1}
    assert h.register_panel("plate", _factory(panel_cls, "plate", made)) is False  # re-register: no rebuild
    assert made["plate"] == 1 and h.tab_titles().count("PLATE") == 1
    h.close()


def test_panel_callbacks_fan_out_from_one_max_callback(qt, main_window, panel_cls, max_rt):
    fake = ctl.FakeController()
    shared.launch_plate(fake, parent=main_window)
    shared.launch_panel(
        "maxgaffer",
        lambda: panel_cls("maxgaffer"),
        on={
            "filePostOpen": lambda w: w.heard.append("open"),
            "postRender": lambda w: w.heard.append("rendered"),
        },
        parent=main_window,
    )
    cb = max_rt.callbacks
    assert [s["event"] for s in cb.ours()] == ["filePostOpen", "postRender"]
    scans = sum(1 for c in fake.calls if c == ("cameras",))

    cb.fire("filePostOpen")
    cb.fire("postRender")
    assert sum(1 for c in fake.calls if c == ("cameras",)) == scans + 1  # PLATE re-read the cameras
    assert shared.get_host().widget("maxgaffer").heard == ["open", "rendered"]
    assert cb.caught == [] and list(shared.get_mux().errors) == []


def test_unload_clears_the_dock_the_panels_and_the_max_callbacks(qt, main_window, panel_cls, max_rt):
    heard = []
    shared.launch_plate(ctl.FakeController(), parent=main_window)
    shared.launch_panel(
        "maxdirector",
        lambda: panel_cls("maxdirector"),
        on={"viewportChange": lambda w: heard.append(1)},
        parent=main_window,
    )
    assert len(max_rt.callbacks.ours()) == 2

    assert shared.unload() is True
    _flush(qt)
    assert max_rt.callbacks.ours() == []
    assert shared.get_host().panels() == [] and shared.get_host().dock is None
    assert _shared_docks(qt, main_window) == []
    max_rt.callbacks.fire("viewportChange")
    assert heard == []
    assert all(shared.get_mux().subscribers(e) == [] for e in shared.EVENTS)


def test_removing_a_panel_takes_its_tab_and_its_callbacks(qt, main_window, panel_cls, max_rt):
    h = shared.get_host()
    shared.launch_panel(
        "plate", lambda: panel_cls("plate"), on={"filePostOpen": lambda w: None}, parent=main_window
    )
    shared.launch_panel(
        "maxgaffer", lambda: panel_cls("mg"), on={"filePostOpen": lambda w: None}, parent=main_window
    )
    assert h.remove_panel("maxgaffer") is True
    assert h.tab_titles() == ["PLATE"]
    assert shared.get_mux().subscribers("filePostOpen") == ["plate"]
    assert h.remove_panel("maxgaffer") is False


def test_closing_the_dock_keeps_the_registrations_and_relaunch_rebuilds(qt, main_window, panel_cls):
    made = {}
    h = shared.DockHost()
    h.register_panel("plate", _factory(panel_cls, "plate", made), on={"postRender": lambda w: None})
    first = h.launch(parent=main_window)
    assert h.mux.subscribers("postRender") == ["plate"]
    h.close()
    _flush(qt)
    assert h.dock is None and h.mux.subscribers("postRender") == []
    second = h.launch(parent=main_window)
    assert second is not first and made == {"plate": 2}
    assert h.mux.subscribers("postRender") == ["plate"]
    h.close()


def test_a_failing_factory_gets_a_placeholder_tab_and_is_retried(qt, main_window, panel_cls, capsys):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("No module named 'maxgaffer.core'")
        return panel_cls("maxgaffer")

    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    h.register_panel("maxgaffer", flaky)
    h.launch(parent=main_window)
    assert h.tab_titles() == ["PLATE", "MaxGaffer"]
    assert h.widget("maxgaffer") is None
    assert "failed to load: ImportError" in h.placeholder_reason("maxgaffer")
    assert "maxgaffer" in h.errors
    assert "failed to load" in capsys.readouterr().out
    plate_page = h.widget("plate")

    h.launch(parent=main_window)
    assert isinstance(h.widget("maxgaffer"), panel_cls)
    assert h.widget("plate") is plate_page  # the healthy tab was left alone
    assert h.tab_titles() == ["PLATE", "MaxGaffer"] and h.placeholder_reason("maxgaffer") == ""
    assert "maxgaffer" not in h.errors
    h.close()


def test_an_open_standalone_dock_blocks_its_twin_tab_until_it_closes(qt, main_window, panel_cls):
    standalone = qt.QtWidgets.QDockWidget("MaxGaffer", main_window)
    standalone.setObjectName("MaxGafferDock")  # what MaxGaffer's own show_dock() names it
    main_window.addDockWidget(qt.QtCore.Qt.RightDockWidgetArea, standalone)
    standalone.show()

    class MaxDirectorDock(qt.QtWidgets.QWidget):  # MaxDirector opens a bare top-level window
        pass

    director = MaxDirectorDock()
    director.show()
    made = {}
    h = shared.DockHost()
    for name in ("plate", "maxgaffer", "maxdirector"):
        h.register_panel(name, _factory(panel_cls, name, made))
    h.launch(parent=main_window)
    assert made == {"plate": 1}
    assert h.widget("maxgaffer") is None and h.widget("maxdirector") is None
    assert "MaxGafferDock" in h.placeholder_reason("maxgaffer")
    assert "MaxDirectorDock" in h.placeholder_reason("maxdirector")
    assert {c.detail for c in h.collisions if c.code == qt_guard.STANDALONE_OPEN} == {
        "maxgaffer: MaxGafferDock",
        "maxdirector: MaxDirectorDock",
    }

    standalone.close()
    director.close()
    h.launch(parent=main_window)
    assert made == {"plate": 1, "maxgaffer": 1, "maxdirector": 1}
    assert h.tab_titles() == ["PLATE", "MaxDirector", "MaxGaffer"]
    h.close()
    standalone.deleteLater()
    director.deleteLater()


def test_a_reloaded_host_adopts_the_shared_dock_instead_of_adding_a_twin(qt, main_window, panel_cls):
    old = shared.DockHost(mux_module.CallbackMux())
    old.register_panel("plate", lambda: panel_cls("plate"))
    dock = old.launch(parent=main_window)
    plate_page = old.widget("plate")

    made = {}
    fresh_mux = mux_module.CallbackMux()
    new = shared.DockHost(fresh_mux)  # importlib.reload(plate_max.shared.host) leaves only the widgets
    new.register_panel(
        "plate", _factory(panel_cls, "plate", made), on={"filePostOpen": lambda w: w.heard.append(1)}
    )
    new.register_panel("maxgaffer", _factory(panel_cls, "maxgaffer", made))
    assert new.launch(parent=main_window) is dock
    assert _shared_docks(qt, main_window) == [dock]
    assert new.widget("plate") is plate_page and made == {"maxgaffer": 1}
    assert new.tab_titles() == ["PLATE", "MaxGaffer"]
    assert fresh_mux.subscribers("filePostOpen") == ["plate"]  # the adopted page is wired again
    fresh_mux.dispatch("filePostOpen")
    assert plate_page.heard == [1]
    new.close()


def test_two_qt_bindings_refuse_the_build(qt, main_window, panel_cls, monkeypatch, capsys):
    pyside2 = types.ModuleType("PySide2")
    pyside2.__file__ = str(Path(sys.prefix) / "PySide2" / "__init__.py")
    monkeypatch.setitem(sys.modules, "PySide2", pyside2)
    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    with pytest.raises(shared.HostError, match=qt_guard.TWO_BINDINGS):
        h.launch(parent=main_window)
    assert h.dock is None and _shared_docks(qt, main_window) == []
    assert [c.code for c in h.collisions if c.fatal] == [qt_guard.TWO_BINDINGS]

    assert (
        shared.launch_panel("plate", lambda: panel_cls("plate"), parent=main_window) is None
    )  # never raises
    assert "shared dock failed to open" in capsys.readouterr().out


def test_without_a_main_window_the_dock_floats_on_its_own(qt, panel_cls):
    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    dock = h.launch()  # off-Max: the fake runtime has no windows.getMAXHWND
    assert dock.parentWidget() is None and dock.isVisible()
    assert h.tab_titles() == ["PLATE"]
    assert h.launch() is dock
    h.close()


def test_without_a_parent_the_ui_packages_max_main_window_is_asked_first(
    qt, main_window, panel_cls, monkeypatch
):
    from plate_max.ui import dock as plate_dock

    monkeypatch.setattr(plate_dock, "max_main_window", lambda: main_window, raising=False)
    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    dock = h.launch()
    assert dock.parentWidget() is main_window and _shared_docks(qt, main_window) == [dock]
    h.close()


def test_without_a_parent_the_hwnd_fallback_finds_the_main_window(qt, main_window, panel_cls, max_rt):
    hwnd = int(main_window.winId())
    max_rt.windows = types.SimpleNamespace(getMAXHWND=lambda: hwnd)  # HYPOTHESIS on the box
    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    dock = h.launch()
    assert dock.parentWidget() is main_window
    h.close()


def test_a_parent_that_is_not_a_main_window_is_ignored(qt, panel_cls, capsys):
    stray = qt.QtWidgets.QWidget()
    h = shared.DockHost()
    h.register_panel("plate", lambda: panel_cls("plate"))
    dock = h.launch(parent=stray)
    assert dock.parentWidget() is None and "not a QMainWindow" in capsys.readouterr().out
    h.close()
    stray.deleteLater()


def test_panel_names_and_handlers_are_validated(qt):
    h = shared.DockHost()
    with pytest.raises(ValueError):
        h.register_panel("Max Gaffer", lambda: None)
    with pytest.raises(TypeError):
        h.register_panel("maxgaffer", "MaxGafferDock")
    with pytest.raises(ValueError, match="unknown events"):
        h.register_panel("maxgaffer", lambda: None, on={"filePreSave": lambda w: None})
    assert h.panels() == []
