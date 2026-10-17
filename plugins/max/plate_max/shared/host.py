"""DockHost: ONE QDockWidget in 3ds Max with a tab per tool (PLATE, MaxDirector, MaxGaffer).

Three plugins, three floating docks, three show_dock() singletons and three sets of callbacks was the
old shape. Here each plugin registers a panel (a name and a zero-argument factory that returns its
widget) and the host builds them as tabs of one dock, parented to Max's main window:

    from plate_max import shared
    shared.launch_panel("maxgaffer", MaxGafferDock, on={"filePostOpen": lambda w: w.refresh_cameras()},
                        parent=qtmax.GetQMaxMainWindow())

Rules the host keeps (and tests/test_shared_bridge.py proves on an offscreen Qt):
  * launch() twice is a no-op: the second call raises the same dock, no factory runs again, no tab
    is added twice; a panel registered after launch appears as a new tab on the open dock;
  * a factory that raises, or a tool whose standalone dock is still open (qt_guard.standalone_open),
    gets a placeholder tab; the next launch() retries just that tab;
  * an existing PlateSharedDock under the main window (a module reload left it there) is adopted, its
    pages kept, never duplicated;
  * two Qt bindings in one process (qt_guard.TWO_BINDINGS) refuse the build; the other binding
    warnings print once per session;
  * a panel's `on` handlers go through the shared CallbackMux under the panel's name, so removing the
    panel (or unload()) removes its callbacks too.

Qt comes from plate_max/ui, the plugin's only Qt boundary (tests/test_stdlib_only.py): launch()
imports plate_max.ui.dock inside the call, exactly as bootstrap.launch() does, and reuses its
QtCore/QtWidgets, so this module imports headless and is never the one that pulls a second Qt binding
into Max. Max's main window is the `parent` the caller passes (qtmax.GetQMaxMainWindow() in each
adapter); without one the host asks plate_max/ui/dock.py for max_main_window() if it has one, then
tries QWidget.find(windows.getMAXHWND()), then falls back to a floating top-level dock (dev, tests).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from ..rt import runtime
from . import qt_guard
from .mux import EVENTS, CallbackMux, get_mux, once_per_session

DOCK_OBJECT_NAME = "PlateSharedDock"
TABS_OBJECT_NAME = "PlateSharedTabs"
PAGE_PREFIX = "plate_shared_panel_"
DOCK_TITLE = "PLATE tools"
DOCK_SIZE = (1040, 1100)  # MaxGaffer's dock is the widest of the three
# Known panels: tab order and titles. Other lowercase names are allowed and go after these.
KNOWN_PANELS = {"plate": "PLATE", "maxdirector": "MaxDirector", "maxgaffer": "MaxGaffer"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NO_QAPP = "no QApplication: run inside 3ds Max (tests and ui_preview create one first)"


class HostError(RuntimeError):
    """The shared dock could not be built (no Qt application, a fatal Qt-binding collision)."""


def _qt() -> tuple[Any, Any]:
    """(QtCore, QtWidgets), borrowed from the plugin's Qt boundary (see the module docstring)."""
    from ..ui import dock as dock_module  # Qt exists only inside Max (and the tests)

    return dock_module.QtCore, dock_module.QtWidgets


def _palette() -> Any:
    from ..ui import dock as dock_module

    return dock_module


def _dock_style() -> str:
    """The dock and its pane in the house palette (plate_max/ui/dock.py, copied from MaxGaffer): flat,
    one hairline, one accent. Selectors are scoped by objectName so nothing leaks into the pages, which
    keep their own stylesheets."""
    d = _palette()
    return (
        f"QDockWidget#{DOCK_OBJECT_NAME}{{background:{d.BG};color:{d.TEXT};}}"
        f"QTabWidget#{TABS_OBJECT_NAME}::pane{{border:none;background:{d.BG};}}"
    )


def _tab_bar_style() -> str:
    """Set on the tab bar itself, so the pages' own tab bars (if any) are untouched."""
    d = _palette()
    return (
        f"QTabBar{{background:{d.BG};}}"
        f"QTabBar::tab{{background:{d.BG};color:{d.DIM};padding:8px 16px;border:none;"
        f"font-weight:600;letter-spacing:1px;}}"
        f"QTabBar::tab:selected{{color:{d.TEXT};border-bottom:2px solid {d.ACCENT};}}"
        f"QTabBar::tab:hover{{color:{d.TEXT};}}"
    )


def _max_main_window(qtwidgets: Any) -> Any:
    """Max's main window when the caller passed none. First choice: a max_main_window() in
    plate_max/ui/dock.py (the Qt boundary, where qtmax.GetQMaxMainWindow() may be called; a T20
    follow-up, SHARED_BRIDGE.md). Fallback, a HYPOTHESIS for the box: QWidget.find on
    windows.getMAXHWND(). None off-Max."""
    from ..ui import dock as dock_module

    finder = getattr(dock_module, "max_main_window", None)
    if callable(finder):
        try:
            found = finder()
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found
    try:
        hwnd = runtime().windows.getMAXHWND()
        widget = qtwidgets.QWidget.find(int(hwnd))
    except Exception:  # noqa: BLE001 off-Max, or the HWND is not a Qt window
        return None
    return widget.window() if widget is not None else None


class _Panel:
    __slots__ = ("name", "factory", "title", "on")

    def __init__(self, name: str, factory: Callable[[], Any], title: str, on: dict[str, Callable]):
        self.name = name
        self.factory = factory
        self.title = title
        self.on = on


class DockHost:
    """The shared dock. get_host() returns the session singleton; tests build their own."""

    def __init__(self, callback_mux: CallbackMux | None = None):
        self._mux = callback_mux
        self._panels: dict[str, _Panel] = {}
        self._pages: dict[str, Any] = {}  # name -> live tab page (a real panel)
        self._placeholders: dict[str, str] = {}  # name -> why the tab is a placeholder
        self._dock: Any = None
        self._tabs: Any = None
        self._qtwidgets: Any = None
        self._launching = False
        self.collisions: list[qt_guard.Collision] = []
        self.errors: dict[str, str] = {}
        self.builds: dict[str, int] = {}  # factory calls per panel (a no-op launch leaves this alone)

    @property
    def mux(self) -> CallbackMux:
        return self._mux if self._mux is not None else get_mux()

    # ----------------------------------------------------------------- registry
    def register_panel(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        title: str | None = None,
        on: Mapping[str, Callable[[Any], Any]] | None = None,
    ) -> bool:
        """Register (or re-register) a panel. factory() returns the tab's widget; on maps a Max event
        (mux.EVENTS) to handler(widget). Re-registering a name only updates what the NEXT build uses:
        a live tab is never rebuilt or duplicated. Returns True the first time a name registers.
        On an open dock a new panel's tab is added at once."""
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"panel name {name!r}: use lowercase letters, digits and _ (max 32)")
        if not callable(factory):
            raise TypeError(f"panel {name!r}: factory is not callable")
        handlers = dict(on or {})
        bad = [e for e in handlers if e not in self.mux.events]
        if bad:
            raise ValueError(f"panel {name!r}: unknown events {bad}; expected {', '.join(EVENTS)}")
        for event, handler in handlers.items():
            if not callable(handler):
                raise TypeError(f"panel {name!r}: handler for {event} is not callable")
        new = name not in self._panels
        panel = _Panel(name, factory, title or KNOWN_PANELS.get(name, name), handlers)
        self._panels[name] = panel
        if new and self._alive():
            if name in self._pages:  # a page adopted from a reloaded host: bind its callbacks again
                self._subscribe(panel, self._pages[name])
            else:
                self._ensure_tab(name)
        return new

    def panels(self) -> list[str]:
        """Registered panel names in tab order: the known three first, then registration order."""
        known = [n for n in KNOWN_PANELS if n in self._panels]
        return known + [n for n in self._panels if n not in KNOWN_PANELS]

    def widget(self, name: str) -> Any:
        """The live page of a panel (None while it is a placeholder or not built)."""
        return self._pages.get(name)

    def placeholder_reason(self, name: str) -> str:
        return self._placeholders.get(name, "")

    def tab_titles(self) -> list[str]:
        if not self._alive():
            return []
        return [self._tabs.tabText(i) for i in range(self._tabs.count())]

    @property
    def dock(self) -> Any:
        return self._dock if self._alive() else None

    # ----------------------------------------------------------------- launch
    def launch(self, parent: Any = None, select: str | None = None) -> Any:
        """Build the dock once, or raise the open one. Placeholder tabs are retried; live tabs are
        left alone. Returns the QDockWidget. Raises HostError when it cannot be built."""
        if self._launching:
            return self._dock
        self._launching = True
        try:
            if not self._alive():
                self._build(parent)
            for name in self.panels():
                if name not in self._pages:
                    self._ensure_tab(name)
            self._dock.show()
            self._dock.raise_()
            if select:
                self.select(select)
            return self._dock
        finally:
            self._launching = False

    def select(self, name: str) -> bool:
        page = self._page_object(name)
        if page is None:
            return False
        self._tabs.setCurrentWidget(page)
        return True

    def _alive(self) -> bool:
        if self._dock is None:
            return False
        try:
            self._dock.objectName()  # raises RuntimeError once the C++ dock is deleted
            return True
        except RuntimeError:
            self._forget_widgets()
            return False

    def _build(self, parent: Any) -> None:
        qtcore, qtwidgets = _qt()
        if qtwidgets.QApplication.instance() is None:
            raise HostError(NO_QAPP)
        found = qt_guard.check_bindings()
        once_per_session("plate.shared.qt_guard", lambda: _report_collisions(found))
        self.collisions = list(found)
        fatal = [c for c in found if c.fatal]
        if fatal:
            raise HostError("; ".join(str(c) for c in fatal))
        if parent is None:
            parent = _max_main_window(qtwidgets)
        if parent is not None and not isinstance(parent, qtwidgets.QMainWindow):
            print(f"[PLATE shared] parent {type(parent).__name__} is not a QMainWindow; the dock floats")
            parent = None
        dock = qt_guard.find_dock(qtwidgets, parent, DOCK_OBJECT_NAME)
        if dock is not None:  # a reload left the shared dock in Max: adopt it, keep its pages
            self._adopt(dock, qtwidgets)
            return
        dock = qtwidgets.QDockWidget(DOCK_TITLE, parent)
        dock.setObjectName(DOCK_OBJECT_NAME)
        dock.setStyleSheet(_dock_style())
        dock.setWidget(self._new_tabs(qtwidgets, dock))
        if parent is not None:
            parent.addDockWidget(qtcore.Qt.RightDockWidgetArea, dock)
            dock.setFloating(True)
        dock.resize(*DOCK_SIZE)
        self._dock, self._tabs, self._qtwidgets = dock, dock.widget(), qtwidgets

    @staticmethod
    def _new_tabs(qtwidgets: Any, dock: Any) -> Any:
        tabs = qtwidgets.QTabWidget(dock)
        tabs.setObjectName(TABS_OBJECT_NAME)
        tabs.setDocumentMode(True)
        tabs.tabBar().setStyleSheet(_tab_bar_style())
        return tabs

    def _adopt(self, dock: Any, qtwidgets: Any) -> None:
        tabs = dock.widget()
        if not isinstance(tabs, qtwidgets.QTabWidget):
            tabs = self._new_tabs(qtwidgets, dock)
            dock.setWidget(tabs)
        self._dock, self._tabs, self._qtwidgets = dock, tabs, qtwidgets
        for i in range(tabs.count()):
            page = tabs.widget(i)
            name = page.objectName()[len(PAGE_PREFIX) :] if page.objectName().startswith(PAGE_PREFIX) else ""
            if not name or page.property("plateSharedPlaceholder"):
                continue
            self._pages[name] = page
            panel = self._panels.get(name)
            if panel is not None:  # the old module's subscriptions died with it: bind them again
                self._subscribe(panel, page)

    # ----------------------------------------------------------------- tabs
    def _page_object(self, name: str) -> Any:
        if not self._alive():
            return None
        for i in range(self._tabs.count()):
            page = self._tabs.widget(i)
            if page.objectName() == PAGE_PREFIX + name:
                return page
        return None

    def _ensure_tab(self, name: str) -> None:
        """Build name's tab, or (re)build its placeholder. A live page is never touched."""
        if name in self._pages:
            return
        panel = self._panels[name]
        standalone = qt_guard.standalone_open(self._qtwidgets, self._dock.parentWidget(), exclude=self._dock)
        if name in standalone:
            reason = (
                f"{panel.title} is open in its own window ({standalone[name]}). Close that window, then "
                "open this dock again to use it here."
            )
            self._record(qt_guard.Collision(qt_guard.STANDALONE_OPEN, f"{name}: {standalone[name]}"))
            self._place(name, self._placeholder(reason), placeholder=reason)
            return
        try:
            page = panel.factory()
            self.builds[name] = self.builds.get(name, 0) + 1
            if page is None:
                raise HostError("the factory returned no widget")
        except Exception as exc:  # noqa: BLE001 one broken tool must not take the dock down
            reason = f"{panel.title} failed to load: {type(exc).__name__}: {exc}"
            self.errors[name] = reason
            print("[PLATE shared] " + reason)
            self._place(name, self._placeholder(reason), placeholder=reason)
            return
        self.errors.pop(name, None)
        self._place(name, page)
        self._pages[name] = page
        self._subscribe(panel, page)

    def _subscribe(self, panel: _Panel, page: Any) -> None:
        for event, handler in panel.on.items():
            self.mux.subscribe(event, lambda h=handler, w=page: h(w), owner=panel.name)

    def _place(self, name: str, page: Any, placeholder: str = "") -> None:
        """Put page in name's slot: replace the old page in place, else insert in tab order."""
        page.setObjectName(PAGE_PREFIX + name)
        page.setProperty("plateSharedPlaceholder", bool(placeholder))
        title = self._panels[name].title
        old = self._page_object(name)
        if old is not None:
            index = self._tabs.indexOf(old)
            current = self._tabs.currentIndex() == index
            self._tabs.removeTab(index)
            old.deleteLater()
            self._tabs.insertTab(index, page, title)
            if current:
                self._tabs.setCurrentIndex(index)
        else:
            order = self.panels()
            later = [self._page_object(n) for n in order[order.index(name) + 1 :]]
            index = next((self._tabs.indexOf(p) for p in later if p is not None), self._tabs.count())
            self._tabs.insertTab(index, page, title)
        if placeholder:
            self._placeholders[name] = placeholder
        else:
            self._placeholders.pop(name, None)

    def _placeholder(self, text: str) -> Any:
        d = _palette()
        label = self._qtwidgets.QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"background:{d.BG};color:{d.DIM};padding:24px;")
        return label

    def _record(self, collision: qt_guard.Collision) -> None:
        if collision not in self.collisions:
            self.collisions.append(collision)

    # ----------------------------------------------------------------- teardown
    def remove_panel(self, name: str) -> bool:
        """Unregister a panel: its tab and page go, and so do its callbacks. False if unknown."""
        if name not in self._panels:
            return False
        self.mux.unsubscribe_owner(name)
        page = self._page_object(name)
        if page is not None:
            self._tabs.removeTab(self._tabs.indexOf(page))
            page.deleteLater()
        self._pages.pop(name, None)
        self._placeholders.pop(name, None)
        self.errors.pop(name, None)
        del self._panels[name]
        return True

    def close(self) -> None:
        """Delete the dock and its pages; registrations stay, so launch() rebuilds every tab."""
        dock = self._dock
        self._forget_widgets()
        if dock is not None:
            try:
                dock.setObjectName("")  # deleteLater is deferred: never let a relaunch adopt this one
                dock.close()
                dock.deleteLater()
            except RuntimeError:
                pass

    def unload(self) -> bool:
        """Everything off: the dock, every registration and the shared Max callbacks (mux.unload).
        Returns what mux.unload() returned."""
        self.close()
        self._panels.clear()
        self.errors.clear()
        self.builds.clear()
        self.collisions = []
        return self.mux.unload()

    def _forget_widgets(self) -> None:
        """The pages are gone (closed, or Max deleted the dock): so are their callbacks."""
        for name in list(self._pages):
            self.mux.unsubscribe_owner(name)
        self._dock = self._tabs = None
        self._pages.clear()
        self._placeholders.clear()


def _report_collisions(found: list[qt_guard.Collision]) -> int:
    for c in found:
        print("[PLATE shared] Qt guard: " + str(c))
    return len(found)


def _report(message: str) -> None:
    try:
        runtime().messageBox(message, title="PLATE")
    except Exception:  # noqa: BLE001 off-Max, or pymxs itself is unusable
        pass
    print("[PLATE shared] " + message)


# --------------------------------------------------------------------------- session API

_state: dict[str, Any] = {"host": None}


def get_host() -> DockHost:
    """The session's one DockHost (every plugin's adapter goes through this)."""
    if _state["host"] is None:
        _state["host"] = DockHost()
    return _state["host"]


def register_panel(name: str, factory: Callable[[], Any], **kw: Any) -> bool:
    return get_host().register_panel(name, factory, **kw)


def launch(parent: Any = None, select: str | None = None) -> Any:
    """Open (or raise) the shared dock. Returns it, or None after a readable message (never raises
    into Max)."""
    try:
        return get_host().launch(parent, select=select)
    except Exception as exc:  # noqa: BLE001
        _report(f"the shared dock failed to open:\n{type(exc).__name__}: {exc}")
        return None


def launch_panel(
    name: str,
    factory: Callable[[], Any],
    *,
    title: str | None = None,
    on: Mapping[str, Callable[[Any], Any]] | None = None,
    parent: Any = None,
) -> Any:
    """What each plugin's adapter calls: register the panel, open the dock on its tab. Calling it again
    (a second macro click) re-raises the same dock and tab. Returns the dock, or None."""
    try:
        get_host().register_panel(name, factory, title=title, on=on)
    except Exception as exc:  # noqa: BLE001
        _report(f"panel {name!r} could not register:\n{type(exc).__name__}: {exc}")
        return None
    return launch(parent, select=name)


def plate_panel(controller: Any = None) -> Callable[[], Any]:
    """The factory for PLATE's own tab: the PlateDock widget of plate_max/ui/dock.py."""

    def build() -> Any:
        from ..ui import dock as dock_module

        return dock_module.PlateDock(controller)

    return build


# PLATE's handlers: a newly opened scene has other cameras.
PLATE_ON: dict[str, Callable[[Any], Any]] = {"filePostOpen": lambda w: w.refresh_cameras()}


def launch_plate(controller: Any = None, parent: Any = None) -> Any:
    """PLATE through the shared dock (what bootstrap.launch() calls once it adopts the host)."""
    return launch_panel("plate", plate_panel(controller), on=PLATE_ON, parent=parent)


def unload() -> bool:
    """Close the shared dock, drop every panel and remove the shared Max callbacks."""
    return get_host().unload()


def _reset() -> None:
    """Forget the singleton without touching Qt or Max (tests only)."""
    _state["host"] = None
