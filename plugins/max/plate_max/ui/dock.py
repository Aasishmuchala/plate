"""PLATE dock: PySide6, instrument-grade dark (the MaxGaffer/MaxDirector house style).

One column: daemon status line, the scene's project (with its custody), camera list, per-camera
recipe editor, the action buttons, the lint panel, progress and log. Send + quote is Send Packet and
then the Edit chain quoted on the canvas for each ingested packet, with the recipe's Engine and Preset
(the only path on which they leave Max); "auto-approve under cap" (off by default) lets the daemon
approve the quotes under the project's per-run cap. Threading contract (copied from MaxGaffer's dock):
  * every pymxs touch happens on Max's MAIN thread, always;
  * slow pure I/O (the daemon health and projects calls, the job poller's daemon calls) runs on a
    QThread while the main thread spins a local QEventLoop, so Max stays responsive and no widget is
    touched off-thread;
  * the daemon calls inside an action this dock starts (Send's hashing, the POST, following the
    ingest) are pumped: the dock hands the action a config whose transport.PUMP_KEY ("_pump") is its
    _pump_events, so transport runs each call on a worker thread while the main thread processes
    events (the controller's _options is wrapped for that; nothing else changes in what it returns);
  * renders block Max by nature, so the log narrates and processes events to never feel dead.

The job poller (plate_max.jobs, made by the controller) runs on this dock's QTimer: it starts with
the dock and stops when the dock closes, so a Light or Scout node on the canvas reaches Max only while
the dock is open. A job marks the dock busy while it runs; a tick waits while the artist is in the
middle of an action.

show_dock() (what bootstrap.launch() calls) opens PLATE as a tab of the shared dock
(plate_max.shared.launch_plate, plugins/max/SHARED_BRIDGE.md) parented to max_main_window();
PLATE_STANDALONE_DOCK=1 keeps the old floating window for side-by-side checks on the box.

Qt is allowed only in this package (tests/test_stdlib_only.py). bootstrap.launch() imports this
module lazily; a missing PySide6 is reported there, never at plate_max import.
"""

from __future__ import annotations

import html as _html
import os
import threading
from typing import Any

from PySide6 import QtCore, QtWidgets  # type: ignore

from .. import policy, transport
from ..config import load_config
from . import controller as ctl

# Apple-dark design language (palette and STYLE copied from MaxGaffer's dock): flat surfaces, ONE
# hairline border, ONE accent colour, no shadows, no gradients. The layout does the talking.
ACCENT = "#0a84ff"  # systemBlue (dark): primary actions, selection, links. The only colour in the app.
BG = "#1c1c1e"  # window
PANEL = "#2c2c2e"  # cards
INSET = "#1c1c1e"  # wells: inputs, lists, log
FILL = "#3a3a3c"  # neutral controls (buttons, popups)
HAIR = "1px solid rgba(255,255,255,0.10)"
TEXT = "#f2f2f7"
DIM = "#98989d"  # secondary text and captions
FAINT = "#636366"  # disabled and placeholders
ERR = "#ff453a"  # systemRed (dark): error emphasis only
WARN = "#ff9f0a"  # systemOrange (dark): one warning only, an unset custody after a send
OK = "#f2f2f7"

STYLE = (
    f"QWidget{{background:{BG};color:{TEXT};font-family:'SF Pro Text','Segoe UI Variable',"
    f"'Segoe UI',Inter;font-size:13px;}}"
    f"QFrame#card{{background:{PANEL};border:{HAIR};border-radius:12px;}}"
    f"QLabel{{background:transparent;}}"
    f"QPushButton{{background:{FILL};border:none;border-radius:8px;padding:7px 14px;color:{TEXT};}}"
    f"QPushButton:hover{{background:#48484a;}}"
    f"QPushButton:pressed{{background:#2c2c2e;}}"
    f"QPushButton:disabled{{background:#2c2c2e;color:{FAINT};}}"
    f"QPushButton#primary{{background:{ACCENT};color:#ffffff;font-weight:600;}}"
    f"QPushButton#primary:hover{{background:#2f95ff;}}"
    f"QPushButton#primary:pressed{{background:#0774e8;}}"
    f"QPushButton#primary:disabled{{background:#2c2c2e;color:{FAINT};}}"
    f"QPushButton#ghost{{background:transparent;border:none;color:{ACCENT};padding:5px 8px;}}"
    f"QPushButton#ghost:hover{{color:#66b3ff;}}"
    f"QPushButton#ghost:pressed{{color:#0774e8;}}"
    f"QPushButton#ghost:disabled{{color:{FAINT};}}"
    f"QLineEdit,QTextEdit,QPlainTextEdit,QSpinBox,QDoubleSpinBox{{background:{INSET};"
    f"border:{HAIR};border-radius:8px;padding:5px 8px;"
    f"selection-background-color:{ACCENT};selection-color:#ffffff;}}"
    f"QSpinBox:disabled{{color:{FAINT};}}"
    f"QComboBox{{background:{FILL};border:none;border-radius:8px;padding:6px 10px;}}"
    f"QComboBox:hover{{background:#48484a;}}"
    f"QComboBox:disabled{{color:{FAINT};}}"
    f"QComboBox::drop-down{{border:none;width:20px;}}"
    f"QComboBox QLineEdit{{background:transparent;border:none;padding:0;}}"
    f"QComboBox QAbstractItemView{{background:{PANEL};border:{HAIR};padding:4px;"
    f"selection-background-color:{ACCENT};selection-color:#ffffff;}}"
    f"QListWidget{{background:{INSET};border:{HAIR};border-radius:10px;padding:4px;"
    f"selection-background-color:{ACCENT};selection-color:#ffffff;}}"
    f"QCheckBox{{background:transparent;spacing:8px;}}"
    f"QCheckBox:disabled{{color:{FAINT};}}"
    f"QCheckBox::indicator{{width:16px;height:16px;border-radius:4px;background:{INSET};border:{HAIR};}}"
    f"QCheckBox::indicator:checked{{background:{ACCENT};border:none;}}"
    f"QProgressBar{{background:{INSET};border:{HAIR};border-radius:6px;height:10px;text-align:center;"
    f"color:{DIM};font-size:10px;}}"
    f"QProgressBar::chunk{{background:{ACCENT};border-radius:5px;}}"
    f"QLabel#dim{{color:{DIM};}}"
    f"QLabel#h{{color:{TEXT};font-weight:600;letter-spacing:3px;}}"
    f"QLabel#cap{{color:{DIM};font-size:11px;font-weight:600;letter-spacing:1px;}}"
    f"QScrollBar:vertical{{background:transparent;width:8px;margin:2px;}}"
    f"QScrollBar::handle:vertical{{background:#48484a;border-radius:4px;min-height:30px;}}"
    f"QScrollBar::add-line,QScrollBar::sub-line{{height:0;}}"
    f"QToolTip{{background:{PANEL};color:{TEXT};border:{HAIR};padding:5px;}}"
)

RECEIVE_LABELS = {"vfb_history": "VFB history", "viewport_background": "Viewport background"}
PENDING_TIP = "Not wired in this build yet."
RECIPE_TIP = (
    "Used by Send + quote: once the packet is ingested, its Edit chain on the canvas is quoted with "
    "this engine and this preset. Send Packet alone does not send them; the canvas's Make bar picks "
    "the engine and the preset for every other edit."
)
SEND_QUOTE_TIP = (
    "Send Packet, then for each packet the daemon ingests: an Edit chain on the canvas (Plate, Mask, "
    "Edit, Judge, Export) quoted with the recipe's engine and preset. Nothing is spent until a quote "
    "is approved."
)
AUTO_APPROVE_TIP = (
    "Let the daemon approve each quote that is under the project's per-run auto cap; anything over "
    "the cap waits for your approval in the canvas."
)
NO_OVERSCAN = "none"
FRAME_MAX = 20000
STANDALONE_ENV = "PLATE_STANDALONE_DOCK"


def _cap(text: str) -> QtWidgets.QLabel:
    """Section caption: the one place small-caps styling lives."""
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("cap")
    return lbl


class _Worker(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _ProgressRelay(QtCore.QObject):
    """Marshals progress callbacks onto the main thread: Qt widgets must never be touched from a
    worker thread. Signal arguments: done, total (0 = unknown), message."""

    progress = QtCore.Signal(int, int, str)


class PlateDock(QtWidgets.QWidget):
    def __init__(self, controller: Any = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PLATE")
        self.setStyleSheet(STYLE)
        self.ctrl = controller if controller is not None else ctl.LiveController()
        self.ctrl.io = self._run_blocking_io  # daemon waits run off-thread, Max stays alive
        self._workers: list[_Worker] = []
        self._busy = False
        self._pumping = False  # inside an action: its daemon calls get config["_pump"]
        self._wrap_options()
        self._loading = False
        self._loading_project = False
        self._io_depth = 0  # local event loops spinning (a daemon wait): the poller holds off
        self._job: dict[str, Any] | None = None  # the canvas job running now
        self._container: Any = None  # the QDockWidget around this page, watched for close/show
        self.poller: Any = None  # made below, once the widgets exist
        self._relay = _ProgressRelay(self)
        self._relay.progress.connect(self._on_progress)
        self.class_checks: dict[str, QtWidgets.QCheckBox] = {}
        self._build()
        self.refresh_cameras()
        self.refresh_status()
        self.refresh_projects()
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setObjectName("plateJobPoller")
        self.poller = self._make_poller()
        self._start_poller()

    # ================================================================= layout
    def _card(self, parent_layout) -> QtWidgets.QVBoxLayout:
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        parent_layout.addWidget(frame)
        return lay

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(0)
        host = QtWidgets.QScrollArea()
        host.setWidgetResizable(True)
        host.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(inner)
        col.setContentsMargins(2, 2, 10, 2)
        col.setSpacing(16)
        host.setWidget(inner)
        outer.addWidget(host)

        # ---- header: wordmark, daemon status, refresh
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        title = QtWidgets.QLabel("PLATE")
        title.setObjectName("h")
        head.addWidget(title)
        head.addStretch(1)
        self.lbl_status = QtWidgets.QLabel("daemon: checking")
        self.lbl_status.setObjectName("dim")
        self.lbl_status.setToolTip("Health and version handshake with the PLATE daemon on this machine.")
        head.addWidget(self.lbl_status)
        self.btn_status = QtWidgets.QPushButton("Check")
        self.btn_status.setObjectName("ghost")
        self.btn_status.clicked.connect(self.refresh_status)
        head.addWidget(self.btn_status)
        col.addLayout(head)

        # ---- project: what the scene sends into, and its custody
        proj = self._card(col)
        proj_head = QtWidgets.QHBoxLayout()
        proj_head.addWidget(_cap("PROJECT"))
        proj_head.addStretch(1)
        self.btn_projects = QtWidgets.QPushButton("Refresh")
        self.btn_projects.setObjectName("ghost")
        self.btn_projects.clicked.connect(self.refresh_projects)
        proj_head.addWidget(self.btn_projects)
        proj.addLayout(proj_head)
        proj_row = QtWidgets.QHBoxLayout()
        proj_row.setSpacing(12)
        self.cb_project = QtWidgets.QComboBox()
        self.cb_project.setEditable(True)
        self.cb_project.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.cb_project.setMinimumWidth(220)
        self.cb_project.setToolTip(
            "The project this scene sends into (the daemon's projects; type a new id to start one). "
            "Kept in <scene>.plate.json next to the .max file."
        )
        self.cb_project.currentIndexChanged.connect(self._on_project_changed)
        self.cb_project.lineEdit().editingFinished.connect(self._on_project_changed)
        proj_row.addWidget(self.cb_project, 1)
        self.lbl_custody = QtWidgets.QLabel("custody unset")
        self.lbl_custody.setObjectName("dim")
        self.lbl_custody.setToolTip("The project's custody: which engine grades its plates may reach.")
        proj_row.addWidget(self.lbl_custody)
        proj.addLayout(proj_row)

        # ---- cameras
        cams = self._card(col)
        cap_row = QtWidgets.QHBoxLayout()
        cap_row.addWidget(_cap("CAMERAS"))
        cap_row.addStretch(1)
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.setObjectName("ghost")
        self.btn_refresh.clicked.connect(self.refresh_cameras)
        cap_row.addWidget(self.btn_refresh)
        cams.addLayout(cap_row)
        self.cam_list = QtWidgets.QListWidget()
        self.cam_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.cam_list.setMinimumHeight(110)
        self.cam_list.setToolTip("Pick the cameras to send. The recipe below edits the focused one.")
        self.cam_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.cam_list.currentItemChanged.connect(self._on_current_changed)
        cams.addWidget(self.cam_list)

        # ---- recipe editor
        rec = self._card(col)
        self.lbl_recipe = _cap("RECIPE")
        rec.addWidget(self.lbl_recipe)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.cb_engine = QtWidgets.QComboBox()
        self.cb_engine.addItems(self.ctrl.engines())
        self.cb_engine.setToolTip(RECIPE_TIP)
        form.addRow("Engine (used by Send + quote)", self.cb_engine)
        self.cb_preset = QtWidgets.QComboBox()
        self.cb_preset.setEditable(True)
        self.cb_preset.addItem("")
        self.cb_preset.addItems(self.ctrl.presets())
        self.cb_preset.setToolTip(RECIPE_TIP)
        form.addRow("Preset (used by Send + quote)", self.cb_preset)
        self.chk_safe = QtWidgets.QCheckBox("Safe frame (the deliverable crop inside the render)")
        form.addRow("", self.chk_safe)
        frame_row = QtWidgets.QHBoxLayout()
        self.sp_safe: dict[str, QtWidgets.QSpinBox] = {}
        for key in ("x", "y", "w", "h"):
            sp = QtWidgets.QSpinBox()
            sp.setRange(0, FRAME_MAX)
            sp.setPrefix(f"{key} ")
            sp.setEnabled(False)
            self.sp_safe[key] = sp
            frame_row.addWidget(sp)
        form.addRow("", frame_row)
        self.cb_overscan = QtWidgets.QComboBox()
        self.cb_overscan.addItem("none (render at the camera aspect)", None)
        for aspect in self.ctrl.overscan_aspects():
            self.cb_overscan.addItem(f"overscan to {aspect}", aspect)
        form.addRow("Overscan", self.cb_overscan)
        rec.addLayout(form)
        rec.addWidget(_cap("EDITABLE CLASSES (everything else stays locked)"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        choices = [c for c in self.ctrl.classes() if c not in policy.NEVER_EDITABLE_CLASSES]
        for n, cls in enumerate(choices):
            box = QtWidgets.QCheckBox(cls)
            self.class_checks[cls] = box
            grid.addWidget(box, n // 3, n % 3)
        rec.addLayout(grid)
        self.cb_engine.currentIndexChanged.connect(self._on_recipe_changed)
        self.cb_preset.editTextChanged.connect(self._on_recipe_changed)
        self.chk_safe.toggled.connect(self._on_safe_toggled)
        for sp in self.sp_safe.values():
            sp.valueChanged.connect(self._on_recipe_changed)
        self.cb_overscan.currentIndexChanged.connect(self._on_recipe_changed)
        for box in self.class_checks.values():
            box.toggled.connect(self._on_recipe_changed)

        # ---- actions
        act = self._card(col)
        act.addWidget(_cap("ACTIONS"))
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(8)
        self.btn_send = self._button("Send Packet", self._on_send, primary=True)
        self.btn_send_quote = self._button("Send + quote", self._on_send_quote)
        self.btn_batch = self._button("Batch Cameras", self._on_batch)
        for b in (self.btn_send, self.btn_send_quote, self.btn_batch):
            row1.addWidget(b)
        row1.addStretch(1)
        act.addLayout(row1)
        row1b = QtWidgets.QHBoxLayout()
        row1b.setSpacing(18)
        self.chk_supersedes = QtWidgets.QCheckBox("supersedes last")
        self.chk_supersedes.setToolTip(
            "The new packet replaces the camera's last packet sent from this dock: approved layers carry "
            "over where the IDs are unchanged."
        )
        self.chk_supersedes.toggled.connect(self._on_supersedes_toggled)
        row1b.addWidget(self.chk_supersedes)
        self.chk_auto_approve = QtWidgets.QCheckBox("auto-approve under cap")
        self.chk_auto_approve.setChecked(False)
        self.chk_auto_approve.setToolTip(AUTO_APPROVE_TIP)
        row1b.addWidget(self.chk_auto_approve)
        row1b.addStretch(1)
        act.addLayout(row1b)
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(8)
        self.btn_motion = self._button("Send Motion", self._on_motion)
        self.btn_light = self._button("Light", self._on_light)
        self.btn_scout = self._button("Scout", self._on_scout)
        for b in (self.btn_motion, self.btn_light, self.btn_scout):
            row2.addWidget(b)
        self.chk_scout_keep = QtWidgets.QCheckBox("keep scout cameras")
        self.chk_scout_keep.setToolTip("Leave the __PLATE_scout_N cameras in the scene after the previews.")
        row2.addWidget(self.chk_scout_keep)
        row2.addStretch(1)
        act.addLayout(row2)
        row3 = QtWidgets.QHBoxLayout()
        row3.setSpacing(8)
        self.btn_receive = self._button("Receive", self._on_receive)
        self.cb_receive = QtWidgets.QComboBox()
        for mode in ctl.RECEIVE_MODES:
            self.cb_receive.addItem(RECEIVE_LABELS.get(mode, mode), mode)
        self.cb_receive.setToolTip("Where a received result is loaded for the in-Max A/B.")
        row3.addWidget(self.btn_receive)
        row3.addWidget(self.cb_receive)
        row3.addStretch(1)
        act.addLayout(row3)

        # ---- lint
        lint = self._card(col)
        lint_head = QtWidgets.QHBoxLayout()
        lint_head.addWidget(_cap("LINT"))
        self.lbl_lint = QtWidgets.QLabel("not checked")
        self.lbl_lint.setObjectName("dim")
        lint_head.addWidget(self.lbl_lint)
        lint_head.addStretch(1)
        self.btn_lint = QtWidgets.QPushButton("Check selected")
        self.btn_lint.setObjectName("ghost")
        self.btn_lint.clicked.connect(self.run_lint)
        lint_head.addWidget(self.btn_lint)
        lint.addLayout(lint_head)
        self.lint_view = QtWidgets.QTextEdit()
        self.lint_view.setReadOnly(True)
        self.lint_view.setMinimumHeight(120)
        self.lint_view.setMaximumHeight(240)
        lint.addWidget(self.lint_view)

        # ---- progress and log
        prog = self._card(col)
        prog_head = QtWidgets.QHBoxLayout()
        prog_head.addWidget(_cap("PROGRESS"))
        prog_head.addStretch(1)
        self.lbl_jobs = QtWidgets.QLabel("canvas jobs: off")
        self.lbl_jobs.setObjectName("dim")
        self.lbl_jobs.setToolTip(
            "Light and Scout nodes on the canvas reach this Max through the dock while it is open."
        )
        prog_head.addWidget(self.lbl_jobs)
        prog.addLayout(prog_head)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        prog.addWidget(self.bar)
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        prog.addWidget(self.log)
        col.addStretch(1)
        self._refresh_buttons()

    def _button(self, text: str, slot, primary: bool = False) -> QtWidgets.QPushButton:
        btn = QtWidgets.QPushButton(text)
        if primary:
            btn.setObjectName("primary")
        btn.clicked.connect(lambda _checked=False: slot())
        return btn

    # ================================================================= plumbing
    def _log(self, msg: str, err: bool = False) -> None:
        """One log line, always as rich text in one container (only its colour says error): a line
        left to Qt's plain-or-rich guess shows &#x27; and &amp; literally."""
        text = _html.escape(str(msg))
        self.log.append(f'<span style="white-space:pre-wrap;color:{ERR if err else TEXT}">{text}</span>')
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())
        QtWidgets.QApplication.processEvents()

    def _run_blocking_io(self, fn):
        """Run pure-I/O fn on a worker while the MAIN thread spins a local event loop: Max stays
        alive, pymxs is never touched off-thread, exceptions re-raise here."""
        loop = QtCore.QEventLoop()
        box: dict[str, Any] = {}
        worker = _Worker(fn)
        self._workers.append(worker)
        worker.done.connect(lambda r: (box.__setitem__("r", r), loop.quit()))
        worker.failed.connect(lambda e: (box.__setitem__("e", e), loop.quit()))
        self._io_depth += 1
        try:
            worker.start()
            loop.exec()
            worker.wait()
        finally:
            self._io_depth -= 1
        self._workers.remove(worker)
        if "e" in box:
            raise RuntimeError(box["e"])
        return box.get("r")

    @staticmethod
    def _pump_events() -> None:
        """transport's pump (run_with_pump): Max's events, from the main thread only."""
        if threading.current_thread() is threading.main_thread():
            QtWidgets.QApplication.processEvents()

    def _wrap_options(self) -> None:
        """Wrap the controller's _options (LiveController; a controller without one is left alone) so
        that, while this dock runs an action, the options carry a config with transport.PUMP_KEY set
        to _pump_events: the action's hash, post and ingest follow then run on a worker thread while
        Max's events are processed. A controller wrapped by an earlier dock is re-wrapped from its
        original, so the open dock is the one asked."""
        current = getattr(self.ctrl, "_options", None)
        original = getattr(current, "plate_original", current)
        if not callable(original):
            return

        def pumped(*args: Any, **kwargs: Any) -> Any:
            opts = original(*args, **kwargs)
            if self._pumping and isinstance(opts, dict):
                cfg = dict(opts.get("config") or load_config())
                cfg[transport.PUMP_KEY] = self._pump_events
                opts["config"] = cfg
            return opts

        pumped.plate_original = original  # type: ignore[attr-defined]
        try:
            self.ctrl._options = pumped
        except (AttributeError, TypeError):  # a controller that takes no attributes keeps its own
            pass

    def _progress(self, done: int, total: int, message: str) -> None:
        """The callback handed to controller actions (safe to call from any thread)."""
        self._relay.progress.emit(int(done), int(total), str(message))

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
        else:
            self.bar.setRange(0, 0)
        if message:
            self._log(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        caps = self.ctrl.capabilities()
        why_not = self._unavailable()
        has_cams = bool(self.selected_cameras())
        table = (
            (self.btn_send, ctl.CAP_SEND_PACKET, True),
            (self.btn_batch, ctl.CAP_BATCH, True),
            (self.btn_motion, ctl.CAP_MOTION, True),
            (self.btn_light, ctl.CAP_LIGHT, True),
            (self.btn_scout, ctl.CAP_SCOUT, False),
            (self.btn_receive, ctl.CAP_RECEIVE, False),
        )
        for btn, cap, needs_camera in table:
            available = cap in caps
            btn.setEnabled(available and not self._busy and (has_cams or not needs_camera))
            btn.setToolTip("" if available else why_not.get(cap) or PENDING_TIP)
        quotes = ctl.CAP_SEND_PACKET in caps and callable(getattr(self.ctrl, "send_and_quote", None))
        self.btn_send_quote.setEnabled(quotes and not self._busy and has_cams)
        self.btn_send_quote.setToolTip(
            SEND_QUOTE_TIP if quotes else why_not.get(ctl.CAP_SEND_PACKET) or PENDING_TIP
        )
        self.chk_auto_approve.setEnabled(quotes and not self._busy)
        self.cb_receive.setEnabled(ctl.CAP_RECEIVE in caps and not self._busy)
        self.chk_scout_keep.setEnabled(ctl.CAP_SCOUT in caps and not self._busy)
        for w in (
            self.btn_refresh,
            self.btn_status,
            self.btn_lint,
            self.cam_list,
            self.cb_project,
            self.btn_projects,
            self.chk_supersedes,
        ):
            w.setEnabled(not self._busy)

    def _unavailable(self) -> dict[str, str]:
        """Why each disabled action is disabled (MAXGAFFER_MISSING for Light without MaxGaffer)."""
        reasons = getattr(self.ctrl, "unavailable", None)
        if not callable(reasons):
            return {}
        try:
            return dict(reasons() or {})
        except Exception:  # noqa: BLE001 a tooltip must never raise into Max
            return {}

    # ================================================================= daemon status
    def refresh_status(self) -> dict[str, Any]:
        try:
            status = self.ctrl.io(self.ctrl.daemon_status)
        except Exception as e:  # noqa: BLE001 the status line must never raise into Max
            status = {"reachable": False, "ok": False, "daemon_version": None, "reason": str(e), "url": ""}
        self.status = status
        if status.get("ok"):
            text = f"daemon {status.get('daemon_version') or '?'} · {status.get('url', '')} · compatible"
            self.lbl_status.setStyleSheet(f"color:{OK};")
        elif status.get("reachable"):
            text = (
                f"daemon {status.get('daemon_version') or '?'} · NOT compatible: {status.get('reason', '')}"
            )
            self.lbl_status.setStyleSheet(f"color:{ERR};")
        else:
            text = f"daemon unreachable: {status.get('reason', '')}"
            self.lbl_status.setStyleSheet(f"color:{ERR};")
        self.lbl_status.setText(text)
        return status

    def _daemon_ready(self) -> bool:
        status = self.refresh_status()
        if not status.get("ok"):
            self._log(f"Not sending: {self.lbl_status.text()}", err=True)
            return False
        return True

    # ================================================================= project binding
    def refresh_projects(self) -> list[dict[str, Any]]:
        """Fill the Project combo from the daemon's projects (off the main thread) and select the
        scene's binding. A daemon that does not answer leaves the binding as it is."""
        heads: list[dict[str, Any]] = []
        try:
            heads = list(self.ctrl.io(self.ctrl.projects) or [])
        except Exception as e:  # noqa: BLE001 the combo must never raise into Max
            self.cb_project.setToolTip(f"projects not loaded: {e}")
        ids = [str(h.get("project_id")) for h in heads if h.get("project_id")]
        self._fill_projects(ids)
        return heads

    def _fill_projects(self, ids: list[str]) -> None:
        current = self._project_id()
        self._loading_project = True
        try:
            self.cb_project.clear()
            for pid in [*ids, *([current] if current and current not in ids else [])]:
                self.cb_project.addItem(pid, pid)
            index = self.cb_project.findData(current)
            if index >= 0:
                self.cb_project.setCurrentIndex(index)
            else:
                self.cb_project.setEditText(current)
        finally:
            self._loading_project = False
        self._show_custody()

    def _project_id(self) -> str:
        try:
            return str(self.ctrl.project_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def sync_project(self) -> None:
        """Show the open scene's binding (another scene may be open now) without asking the daemon."""
        current = self._project_id()
        if current and self.cb_project.currentText().strip() != current:
            self._loading_project = True
            try:
                index = self.cb_project.findData(current)
                if index < 0:
                    self.cb_project.addItem(current, current)
                    index = self.cb_project.findData(current)
                self.cb_project.setCurrentIndex(index)
            finally:
                self._loading_project = False
        self._show_custody()

    def _on_project_changed(self, *_args) -> None:
        if self._loading_project:
            return
        self.choose_project(self.cb_project.currentText())

    def choose_project(self, project_id: str) -> bool:
        """Bind the scene to project_id (the combo's pick or typed text) and keep it in
        <scene>.plate.json. False (and the old binding shown again) for text that is no project id."""
        text = str(project_id or "").strip()
        if not text or text == self._project_id():
            return False
        try:
            saved = self.ctrl.set_project(text)
        except Exception as e:  # noqa: BLE001 a bad id is shown, never raised into Max
            self._log(f"Project: {e}", err=True)
            self.sync_project()
            return False
        pid = self._project_id()
        where = f"saved in {saved}" if saved else "this scene is unsaved: kept until another scene opens"
        self._log(f"Project: {pid} ({where})")
        self.sync_project()
        return True

    def _custody(self) -> dict[str, Any] | None:
        try:
            return self.ctrl.project_custody()
        except Exception:  # noqa: BLE001
            return None

    def _show_custody(self, after_send: bool = False) -> None:
        custody = self._custody()
        unset = not custody or bool(custody.get("unset"))
        if unset and after_send:
            self.lbl_custody.setText(ctl.CUSTODY_UNSET)
            self.lbl_custody.setStyleSheet(f"color:{WARN};")
            return
        self.lbl_custody.setText(ctl.custody_text(custody))
        self.lbl_custody.setStyleSheet(f"color:{DIM};")

    # ================================================================= cameras and recipe
    def selected_cameras(self) -> list[str]:
        return [i.data(QtCore.Qt.UserRole) for i in self.cam_list.selectedItems()]

    def current_camera(self) -> str:
        item = self.cam_list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else ""

    def refresh_cameras(self) -> None:
        keep = set(self.selected_cameras())
        current = self.current_camera()
        self.cam_list.blockSignals(True)
        self.cam_list.clear()
        try:
            cams = self.ctrl.cameras()
        except Exception as e:  # noqa: BLE001
            self._log(f"camera scan failed: {e}", err=True)
            cams = []
        for cam in cams:
            label = cam["name"] + ("  (hidden)" if cam.get("hidden") else "")
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, cam["name"])
            self.cam_list.addItem(item)
            if cam["name"] in keep:
                item.setSelected(True)
            if cam["name"] == current:
                self.cam_list.setCurrentItem(item)
        self.cam_list.blockSignals(False)
        if self.cam_list.currentItem() is None and self.cam_list.count():
            self.cam_list.setCurrentRow(0)  # signals are live again: loads the recipe and the buttons
        self._on_current_changed()
        if hasattr(self, "cb_project"):  # a new scene (filePostOpen) may be bound to another project
            self.sync_project()
        self._refresh_buttons()

    def _on_selection_changed(self) -> None:
        self._refresh_buttons()

    def _on_current_changed(self, *_args) -> None:
        self._load_recipe(self.current_camera())

    def _load_recipe(self, camera: str) -> None:
        """Fill the editor from the controller's recipe for camera (no write-back while loading)."""
        self._loading = True
        try:
            for w in (
                self.cb_engine,
                self.cb_preset,
                self.cb_overscan,
                self.chk_safe,
                *self.class_checks.values(),
                *self.sp_safe.values(),
            ):
                w.setEnabled(bool(camera))
            self.lbl_recipe.setText(f"RECIPE  ·  {camera}" if camera else "RECIPE")
            if not camera:
                return
            rec = self.ctrl.recipe(camera)
            engine = str(rec.get("engine_id") or "")
            idx = self.cb_engine.findText(engine)
            if idx < 0 and engine:
                self.cb_engine.addItem(engine)
                idx = self.cb_engine.findText(engine)
            self.cb_engine.setCurrentIndex(max(idx, 0))
            self.cb_preset.setEditText(str(rec.get("preset") or ""))
            frame = rec.get("safe_frame")
            self.chk_safe.setChecked(bool(frame))
            for key, sp in self.sp_safe.items():
                sp.setValue(int((frame or {}).get(key, 0)))
                sp.setEnabled(bool(frame))
            aspect = rec.get("overscan_aspect")
            idx = self.cb_overscan.findData(aspect)
            if idx < 0 and aspect:
                self.cb_overscan.addItem(f"overscan to {aspect}", aspect)
                idx = self.cb_overscan.findData(aspect)
            self.cb_overscan.setCurrentIndex(max(idx, 0))
            wanted = set(rec.get("editable_classes") or [])
            for cls, box in self.class_checks.items():
                box.setChecked(cls in wanted)
        finally:
            self._loading = False

    def recipe_from_controls(self, camera: str) -> dict[str, Any]:
        rec = dict(self.ctrl.recipe(camera))
        rec["engine_id"] = self.cb_engine.currentText()
        rec["preset"] = self.cb_preset.currentText().strip()
        rec["safe_frame"] = (
            {key: int(sp.value()) for key, sp in self.sp_safe.items()} if self.chk_safe.isChecked() else None
        )
        rec["overscan_aspect"] = self.cb_overscan.currentData()
        rec["editable_classes"] = [cls for cls, box in self.class_checks.items() if box.isChecked()]
        return rec

    def _on_safe_toggled(self, on: bool) -> None:
        for sp in self.sp_safe.values():
            sp.setEnabled(bool(on) and bool(self.current_camera()))
        self._on_recipe_changed()

    def _on_recipe_changed(self, *_args) -> None:
        camera = self.current_camera()
        if self._loading or not camera:
            return
        self.ctrl.set_recipe(camera, self.recipe_from_controls(camera))

    # ================================================================= lint
    def run_lint(self) -> bool:
        """Lint every selected camera; show the report. True when all pass (False with none selected)."""
        cams = self.selected_cameras()
        if not cams:
            self.lbl_lint.setText("select a camera")
            return False
        blocks, all_ok = [], True
        for cam in cams:
            try:
                report = self.ctrl.lint(cam)
            except Exception as e:  # noqa: BLE001
                report = {
                    "passed": False,
                    "unlabelled_share": None,
                    "problems": [{"code": "LINT_ERROR", "node": cam, "detail": f"{type(e).__name__}: {e}"}],
                }
            ok = bool(report.get("passed"))
            all_ok = all_ok and ok
            share = report.get("unlabelled_share")
            tail = f"  (unlabelled {share:.1%})" if isinstance(share, (int, float)) else ""
            color = OK if ok else ERR
            lines = [f'<b style="color:{color}">{_html.escape(cam)}  {"PASS" if ok else "FAIL"}</b>{tail}']
            for p in report.get("problems", []):
                where = f" · {_html.escape(str(p.get('node')))}" if p.get("node") else ""
                lines.append(
                    f'<span style="color:{ERR}">{_html.escape(str(p.get("code")))}</span>{where} · '
                    f"{_html.escape(str(p.get('detail')))}"
                )
            for w in report.get("warnings", []):  # not blocking: a camera Send levels and crops
                where = f" · {_html.escape(str(w.get('node')))}" if w.get("node") else ""
                lines.append(
                    f'<span style="color:{DIM}">{_html.escape(str(w.get("code")))}</span>{where} · '
                    f"{_html.escape(str(w.get('detail')))}"
                )
            blocks.append("<br>".join(lines))
        self.lint_view.setHtml("<br><br>".join(blocks))
        self.lbl_lint.setText("all selected cameras pass" if all_ok else "blocked: fix the problems below")
        return all_ok

    # ================================================================= actions
    def _poller_active(self) -> bool:
        return bool(self.poller is not None and getattr(self.poller, "active", False))

    def _can_start(self, name: str) -> bool:
        """False (said in the log) while a canvas job holds Max; a busy dock ignores the click."""
        if self._busy:
            return False
        if self._poller_active():
            self._log(f"{name}: a canvas job is talking to the daemon; try again in a moment.", err=True)
            return False
        return True

    def _guarded(self, name: str, work) -> None:
        """Run work() with the busy lifecycle; an exception is logged, never raised into Max. The
        action's daemon calls are pumped meanwhile (_wrap_options): the Send flow passes the pump."""
        if self._busy:
            return
        self._set_busy(True)
        self._pumping = True
        try:
            work()
        except Exception as e:  # noqa: BLE001
            self._log(f"{name} failed: {type(e).__name__}: {e}", err=True)
        finally:
            self._pumping = False
            self._set_busy(False)

    def _show_results(self, name: str, results: list[dict[str, Any]], sends: bool = True) -> None:
        """One log line per result (Sent, ingesting / Already sent / Ingest failed: ...). After a send
        to a project whose custody is unset, the custody label says so in amber."""
        if not results:
            self._log(f"{name}: nothing was sent")
        for r in results:
            line, err = ctl.result_message(name, r)
            self._log(line, err=err)
            for warning in r.get("warnings") or []:
                self._log(f"{name}: {r.get('camera')}: {warning}")
            if r.get("quote_line"):
                line, err = ctl.quote_message(name, r)
                self._log(line, err=err)
        if sends and any(r.get("ok") and r.get("packet_id") for r in results):
            self.refresh_projects()  # the first packet may have just created the project, custody unset
            self._show_custody(after_send=True)
            if self.lbl_custody.text() == ctl.CUSTODY_UNSET:
                self._log(
                    f"{name}: project {self._project_id()} has its custody unset: only C0 (local) engines "
                    "run until it is set in the canvas's Project panel"
                )

    def _send_flow(self, name: str, runner) -> None:
        cams = self.selected_cameras()
        if not cams:
            self._log("Select at least one camera.", err=True)
            return
        if not self._can_start(name):
            return
        if not self._daemon_ready():
            return
        if not self.run_lint():
            self._log(f"{name} blocked by lint; fix the problems listed above.", err=True)
            return

        def work():
            self._log(f"{name}: {', '.join(cams)}")
            self._show_results(name, runner(cams, self._progress))

        self._guarded(name, work)

    def _on_send(self) -> None:
        self._send_flow("Send Packet", self.ctrl.send_packet)

    def _on_send_quote(self) -> None:
        """Send Packet, then the Edit chain of each ingested packet quoted (auto-approved under the
        project's cap only when the box is ticked)."""
        auto = self.chk_auto_approve.isChecked()
        self._send_flow(
            "Send + quote", lambda cams, prog: self.ctrl.send_and_quote(cams, prog, auto_approve=auto)
        )

    def _on_batch(self) -> None:
        self._send_flow("Batch Cameras", self.ctrl.batch_cameras)

    def _on_receive(self) -> None:
        if not self._can_start("Receive"):
            return
        path = self._pick_receive_path()
        if not path:
            return
        mode = self.cb_receive.currentData()

        def work():
            ok, message = self.ctrl.receive(path, mode)
            self._log(f"Receive: {message}", err=not ok)

        self._guarded("Receive", work)

    def _pick_receive_path(self) -> str:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Receive a result", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.exr);;All files (*)"
        )
        return path

    def _focused_camera(self) -> str:
        return self.current_camera() or (self.selected_cameras() or [""])[0]

    def _camera_action(self, name: str, runner, camera: str | None = None) -> None:
        camera = camera or self._focused_camera()
        if not camera:
            self._log("Select a camera first.", err=True)
            return
        if not self._can_start(name):
            return
        if not self._daemon_ready():
            return

        def work():
            self._show_results(name, runner(camera, self._progress))

        self._guarded(name, work)

    def _on_motion(self) -> None:
        """Send Motion of the focused camera, from its hero still: the last packet this dock sent of
        it, else the one the artist picks from the camera's stills in the packets folder."""
        camera = self._focused_camera()
        if not camera:
            self._log("Select a camera first.", err=True)
            return
        if not self._can_start("Send Motion"):
            return
        hero = self.ctrl.last_packet(camera) or self._pick_hero_packet(camera)
        if not hero:
            return
        self._log(f"Send Motion: {camera} from hero still {hero}")
        self._camera_action(
            "Send Motion", lambda cam, prog: self.ctrl.send_motion(cam, prog, hero_packet_id=hero), camera
        )

    def _pick_hero_packet(self, camera: str) -> str:
        """The hero still for Send Motion when this dock has sent none of camera: the artist picks one
        of the camera's stills (newest first) or pastes a packet id. "" when cancelled or none exists."""
        choices = list(self.ctrl.hero_candidates(camera) or [])
        if not choices:
            self._log(f"Send Motion needs a hero still of {camera}: Send Packet first.", err=True)
            return ""
        text, ok = QtWidgets.QInputDialog.getItem(
            self, "Hero still", f"Hero still packet for {camera}", choices, 0, True
        )
        return str(text).strip() if ok else ""

    def _on_light(self) -> None:
        """MaxGaffer's light match of the focused camera to a reference picture, then a re-rendered
        packet that supersedes the old one."""
        camera = self._focused_camera()
        if not camera:
            self._log("Select a camera first.", err=True)
            return
        if not self._can_start("Light"):
            return
        reference = self._pick_reference_path()
        if not reference:
            return
        self._camera_action(
            "Light", lambda cam, prog: self.ctrl.light(cam, prog, reference_path=reference), camera
        )

    def _pick_reference_path(self) -> str:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Reference picture for the light match",
            "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.exr);;All files (*)",
        )
        return path

    def _on_scout(self) -> None:
        if not self._can_start("Scout"):
            return
        if not self._daemon_ready():
            return
        keep = self.chk_scout_keep.isChecked()

        def work():
            self._show_results("Scout", self.ctrl.scout(self._progress, keep=keep), sends=False)

        self._guarded("Scout", work)
        if keep:
            self.refresh_cameras()

    def _on_supersedes_toggled(self, on: bool) -> None:
        setter = getattr(self.ctrl, "set_supersede_last", None)
        if callable(setter):
            setter(bool(on))

    # ================================================================= canvas jobs
    def _make_poller(self) -> Any:
        maker = getattr(self.ctrl, "make_poller", None)
        if not callable(maker):
            return None
        try:
            return maker(
                log=self._log,
                busy=self._poller_should_wait,
                on_start=self._on_job_start,
                on_finish=self._on_job_finish,
            )
        except Exception as e:  # noqa: BLE001 no poller is a dock without canvas jobs, not a crash
            self._log(f"canvas jobs off: {type(e).__name__}: {e}", err=True)
            return None

    def _start_poller(self) -> None:
        if self.poller is None:
            self.lbl_jobs.setText("canvas jobs: off")
            return
        try:
            if not self.poller.running:
                self.poller.start(self._poll_timer)
        except RuntimeError:  # the timer went with a closing dock
            return
        except Exception as e:  # noqa: BLE001 a poller that cannot start is a dock without canvas jobs
            self._log(f"canvas jobs off: {type(e).__name__}: {e}", err=True)
            return
        self.lbl_jobs.setText("canvas jobs: on")

    def _stop_poller(self) -> None:
        if self.poller is None:
            return
        try:
            self.poller.stop()
            self.lbl_jobs.setText("canvas jobs: off (dock closed)")
        except RuntimeError:
            pass

    def _poller_should_wait(self) -> bool:
        """A tick waits while the artist is in an action, a daemon wait spins events, or a dialog is up."""
        if self._busy or self._io_depth > 0:
            return True
        return QtWidgets.QApplication.activeModalWidget() is not None

    def _on_job_start(self, job: dict[str, Any]) -> None:
        self._job = dict(job)
        self._set_busy(True)
        what = job.get("camera") or job.get("packet_id") or ""
        self._log(f"Canvas job {job.get('action')} {what}".rstrip())

    def _on_job_finish(self, job: dict[str, Any], body: dict[str, Any]) -> None:
        self._job = None
        self._set_busy(False)
        action = job.get("action")
        if body.get("ok"):
            tail = f" (packet {body['packet_id']})" if body.get("packet_id") else ""
            self._log(f"Canvas job {action}: done{tail}")
        else:
            self._log(f"Canvas job {action} FAILED: {body.get('detail') or 'no reason given'}", err=True)
        if action == "scout" and job.get("keep"):
            self.refresh_cameras()

    def shutdown(self) -> None:
        """Stop the job poller and let the daemon workers finish (closing, or close_dock())."""
        self._stop_poller()
        for worker in list(self._workers):
            worker.wait(2000)

    def _watch_container(self) -> None:
        """Follow the QDockWidget around this page (the shared dock, or the standalone one): the
        poller stops when it closes and starts again when it shows."""
        if self._container is not None:
            return
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, QtWidgets.QDockWidget):
            widget = widget.parentWidget()
        if widget is None:
            return
        self._container = widget
        widget.visibilityChanged.connect(self._on_container_visible)

    def _on_container_visible(self, visible: bool) -> None:
        if visible:
            self._start_poller()
        else:
            self._stop_poller()

    def showEvent(self, event) -> None:  # noqa: N802 Qt naming
        super().showEvent(event)
        self._watch_container()
        self._start_poller()

    def closeEvent(self, event) -> None:  # noqa: N802 Qt naming
        self.shutdown()
        super().closeEvent(event)


_dock_instance: PlateDock | None = None
_dock_container: Any = None
_dock_shared = False  # _dock_instance is the PLATE tab of the shared dock


def max_main_window() -> Any:
    """3ds Max's main window (qtmax.GetQMaxMainWindow()), None off-Max. plate_max.shared.host asks
    this first when an adapter passes no parent (SHARED_BRIDGE.md, PLATE adoption step 1)."""
    try:
        import qtmax  # Max 2021+; absent off-Max

        return qtmax.GetQMaxMainWindow()
    except Exception:  # noqa: BLE001
        return None


def _alive(widget: Any) -> bool:
    if widget is None:
        return False
    try:
        widget.objectName()  # raises RuntimeError once the C++ widget is deleted
        return True
    except RuntimeError:
        return False


def show_dock(controller: Any = None, standalone: bool | None = None) -> PlateDock | None:
    """Open (or raise) the dock: what bootstrap.launch() calls. By default PLATE is a tab of the
    shared dock (shared.launch_plate(controller, parent=max_main_window()): one QDockWidget and one
    set of Max callbacks for PLATE, MaxDirector and MaxGaffer) and the PlateDock page is returned, or
    None when the shared dock could not open (the reason is in a message box and the Listener).
    standalone=True, or PLATE_STANDALONE_DOCK=1, opens the old floating dock instead (a QDockWidget
    on Max's main window, a top-level window off-Max). One instance per session either way."""
    global _dock_instance, _dock_container, _dock_shared
    if standalone is None:
        standalone = os.environ.get(STANDALONE_ENV, "").strip() not in ("", "0")
    if _dock_instance is not None and not _alive(_dock_instance):
        _dock_instance = _dock_container = None
        _dock_shared = False
    if _dock_instance is not None and (standalone or not _dock_shared):
        try:  # one PLATE dock: a standalone one already open is raised, never twinned
            (_dock_container or _dock_instance).show()
            (_dock_container or _dock_instance).raise_()
            return _dock_instance
        except RuntimeError:  # the C++ object is gone (dock closed and deleted)
            _dock_instance = _dock_container = None
            _dock_shared = False
    if not standalone:
        return _show_shared(controller)
    return _show_standalone(controller)


def _show_shared(controller: Any) -> PlateDock | None:
    global _dock_instance, _dock_container, _dock_shared
    from .. import shared

    dock = shared.launch_plate(controller, parent=max_main_window())
    page = shared.get_host().widget("plate") if dock is not None else None
    if page is None:  # the dock failed to open, or PLATE's tab is a placeholder saying why
        return None
    _dock_instance, _dock_container, _dock_shared = page, dock, True
    return page


def _show_standalone(controller: Any) -> PlateDock:
    global _dock_instance, _dock_container, _dock_shared
    parent = max_main_window()
    ctrl = controller if controller is not None else ctl.LiveController()
    if parent is not None:
        dock = QtWidgets.QDockWidget("PLATE", parent)
        dock.setObjectName("PlateDock")
        widget = PlateDock(ctrl, dock)
        dock.setWidget(widget)
        parent.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        dock.setFloating(True)
        dock.resize(580, 1000)
        dock.show()
        _dock_container = dock
    else:
        widget = PlateDock(ctrl)
        widget.resize(580, 1000)
        widget.show()
        _dock_container = None
    _dock_instance, _dock_shared = widget, False
    return widget


def close_dock() -> None:
    """Drop the singleton (tests, and a clean re-launch after an update): the poller stops, and the
    PLATE tab leaves the shared dock (the dock itself closes when no other tool's tab is left)."""
    global _dock_instance, _dock_container, _dock_shared
    page, container, was_shared = _dock_instance, _dock_container, _dock_shared
    _dock_instance = _dock_container = None
    _dock_shared = False
    if _alive(page):
        try:
            page.shutdown()
        except RuntimeError:
            pass
    if was_shared:
        from .. import shared

        host = shared.get_host()
        if host.widget("plate") is page:
            host.remove_panel("plate")
            if not host.panels():
                host.close()
            return
        target = container  # a host that was reset underneath us: close what we opened
    else:
        target = container or page
    if _alive(target):
        try:
            target.close()
            target.deleteLater()
        except RuntimeError:
            pass
