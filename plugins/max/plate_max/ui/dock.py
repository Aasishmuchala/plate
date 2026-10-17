"""PLATE dock: PySide6, instrument-grade dark (the MaxGaffer/MaxDirector house style).

One column: daemon status line, camera list, per-camera recipe editor, the action buttons, the
lint panel, progress and log. Threading contract (copied from MaxGaffer's dock):
  * every pymxs touch happens on Max's MAIN thread, always;
  * slow pure I/O (the daemon health call) runs on a QThread while the main thread spins a local
    QEventLoop, so Max stays responsive and no widget is touched off-thread;
  * renders block Max by nature, so the log narrates and processes events to never feel dead.

Qt is allowed only in this package (tests/test_stdlib_only.py). bootstrap.launch() imports this
module lazily; a missing PySide6 is reported there, never at plate_max import.
"""

from __future__ import annotations

import html as _html
from typing import Any

from PySide6 import QtCore, QtWidgets  # type: ignore

from .. import policy
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
NO_OVERSCAN = "none"
FRAME_MAX = 20000


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
        self._loading = False
        self._relay = _ProgressRelay(self)
        self._relay.progress.connect(self._on_progress)
        self.class_checks: dict[str, QtWidgets.QCheckBox] = {}
        self._build()
        self.refresh_cameras()
        self.refresh_status()

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
        form.addRow("Engine", self.cb_engine)
        self.cb_preset = QtWidgets.QComboBox()
        self.cb_preset.setEditable(True)
        self.cb_preset.addItem("")
        self.cb_preset.addItems(self.ctrl.presets())
        form.addRow("Preset", self.cb_preset)
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
        self.btn_batch = self._button("Batch Cameras", self._on_batch)
        for b in (self.btn_send, self.btn_batch):
            row1.addWidget(b)
        row1.addStretch(1)
        act.addLayout(row1)
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(8)
        self.btn_motion = self._button("Send Motion", self._on_motion)
        self.btn_light = self._button("Light", self._on_light)
        self.btn_scout = self._button("Scout", self._on_scout)
        for b in (self.btn_motion, self.btn_light, self.btn_scout):
            row2.addWidget(b)
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
        prog.addWidget(_cap("PROGRESS"))
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
        text = _html.escape(str(msg))
        self.log.append(f'<span style="color:{ERR}">{text}</span>' if err else text)
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
        worker.start()
        loop.exec()
        worker.wait()
        self._workers.remove(worker)
        if "e" in box:
            raise RuntimeError(box["e"])
        return box.get("r")

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
            btn.setToolTip("" if available else PENDING_TIP)
        self.cb_receive.setEnabled(ctl.CAP_RECEIVE in caps and not self._busy)
        for w in (self.btn_refresh, self.btn_status, self.btn_lint, self.cam_list):
            w.setEnabled(not self._busy)

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
            blocks.append("<br>".join(lines))
        self.lint_view.setHtml("<br><br>".join(blocks))
        self.lbl_lint.setText("all selected cameras pass" if all_ok else "blocked: fix the problems below")
        return all_ok

    # ================================================================= actions
    def _guarded(self, name: str, work) -> None:
        """Run work() with the busy lifecycle; an exception is logged, never raised into Max."""
        if self._busy:
            return
        self._set_busy(True)
        try:
            work()
        except Exception as e:  # noqa: BLE001
            self._log(f"{name} failed: {type(e).__name__}: {e}", err=True)
        finally:
            self._set_busy(False)

    def _show_results(self, name: str, results: list[dict[str, Any]]) -> None:
        if not results:
            self._log(f"{name}: nothing was sent")
        for r in results:
            if r.get("ok"):
                self._log(f"{name}: {r.get('camera')} sent (packet {r.get('packet_id') or '?'})")
            else:
                why = "; ".join(r.get("errors") or []) or "no reason given"
                self._log(f"{name}: {r.get('camera')} FAILED: {why}", err=True)

    def _send_flow(self, name: str, runner) -> None:
        cams = self.selected_cameras()
        if not cams:
            self._log("Select at least one camera.", err=True)
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

    def _on_batch(self) -> None:
        self._send_flow("Batch Cameras", self.ctrl.batch_cameras)

    def _on_receive(self) -> None:
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

    def _camera_action(self, name: str, runner) -> None:
        camera = self.current_camera() or (self.selected_cameras() or [""])[0]
        if not camera:
            self._log("Select a camera first.", err=True)
            return
        if not self._daemon_ready():
            return

        def work():
            self._show_results(name, runner(camera, self._progress))

        self._guarded(name, work)

    def _on_motion(self) -> None:
        self._camera_action("Send Motion", self.ctrl.send_motion)

    def _on_light(self) -> None:
        self._camera_action("Light", self.ctrl.light)

    def _on_scout(self) -> None:
        if not self._daemon_ready():
            return

        def work():
            self._show_results("Scout", self.ctrl.scout(self._progress))

        self._guarded("Scout", work)

    def closeEvent(self, event) -> None:  # noqa: N802 Qt naming
        for worker in list(self._workers):
            worker.wait(2000)
        super().closeEvent(event)


_dock_instance: PlateDock | None = None
_dock_container: Any = None


def show_dock(controller: Any = None) -> PlateDock:
    """Create (or raise) the dock: a floating QDockWidget parented to 3ds Max's main window when
    qtmax is available, else a plain top-level window (dev and tests). One instance per session."""
    global _dock_instance, _dock_container
    if _dock_instance is not None:
        try:
            (_dock_container or _dock_instance).show()
            (_dock_container or _dock_instance).raise_()
            return _dock_instance
        except RuntimeError:  # the C++ object is gone (dock closed and deleted)
            _dock_instance = _dock_container = None
    parent = None
    try:
        import qtmax  # Max 2021+; absent off-Max

        parent = qtmax.GetQMaxMainWindow()
    except Exception:  # noqa: BLE001
        parent = None
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
    _dock_instance = widget
    return widget


def close_dock() -> None:
    """Drop the singleton (tests, and a clean re-launch after an update)."""
    global _dock_instance, _dock_container
    target = _dock_container or _dock_instance
    _dock_instance = _dock_container = None
    if target is not None:
        try:
            target.close()
            target.deleteLater()
        except RuntimeError:
            pass
