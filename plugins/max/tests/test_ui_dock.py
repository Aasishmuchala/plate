"""The PLATE dock driven for real, offscreen, against a FakeController.

Two halves. The controller half (LiveController, the recipe defaults, the FakeController itself) needs
no Qt and always runs. The dock half builds the real PySide6 widgets with QT_QPA_PLATFORM=offscreen and
clicks through every user journey (send, batch, receive, lint block, recovery from a failing action);
it skips cleanly when the PySide6 wheel is not installed (the `qt` fixture). What this proves that an
import smoke cannot: the buttons reach the controller with the right arguments, the busy lifecycle
restores the buttons, daemon I/O leaves the main thread while pymxs-facing calls stay on it, and the
dock paints a non-trivial image. What it cannot prove: how it looks inside 3ds Max's Qt (verify-on-box).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before anything imports Qt

from plate_max import PACKET_VERSION, PLUGIN_VERSION, bootstrap, policy  # noqa: E402
from plate_max.ui import controller as ctl  # noqa: E402

PLUGIN = Path(ctl.__file__).resolve().parent.parent.parent
ALL_CAPS = (
    ctl.CAP_SEND_PACKET,
    ctl.CAP_BATCH,
    ctl.CAP_RECEIVE,
    ctl.CAP_MOTION,
    ctl.CAP_LIGHT,
    ctl.CAP_SCOUT,
)


# ===================================================================== controller half (no Qt)
def test_importing_the_ui_package_and_controller_pulls_in_no_qt():
    code = (
        "import sys; sys.modules['PySide6'] = None; sys.modules['qtmax'] = None; "
        "import plate_max.bootstrap, plate_max.ui, plate_max.ui.controller"
    )
    env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_editable_class_choices_never_offer_a_locked_class():
    choices = ctl.editable_class_choices()
    assert choices, "the recipe editor needs classes to offer"
    assert not set(choices) & set(policy.NEVER_EDITABLE_CLASSES)
    assert len(choices) == len(set(choices))
    assert {"vegetation", "sky", "people", "water"} <= set(choices)


def test_default_editable_classes_follow_the_policy_prefixes():
    default = ctl.default_editable_classes()
    assert {"vegetation", "sky", "people", "water", "furniture"} <= set(default)
    assert not set(default) & set(policy.NEVER_EDITABLE_CLASSES)
    assert "ground" not in default  # HARD_ layers are locked by default
    assert set(default) <= set(ctl.editable_class_choices())


def test_default_recipe_has_the_camera_recipe_field_set():
    rec = ctl.default_recipe("CAM_x")
    assert set(rec) == {
        "camera",
        "engine_id",
        "preset",
        "safe_frame",
        "overscan_aspect",
        "editable_classes",
        "extra_elements",
    }
    assert rec["camera"] == "CAM_x"
    assert rec["safe_frame"] is None and rec["overscan_aspect"] is None
    assert rec["engine_id"] in ctl.ENGINE_IDS


def test_menus_are_the_documented_ones():
    fake = ctl.FakeController()
    assert fake.engines() == list(ctl.ENGINE_IDS) and "nano-banana-2.1" in fake.engines()
    assert {"night-relight", "sunburst-rephotograph", "crop-pair-pushin"} <= set(fake.presets())
    assert "16:9" in fake.overscan_aspects()
    assert ctl.RECEIVE_MODES == ("vfb_history", "viewport_background")


def test_result_dict_reads_dicts_and_objects_and_a_bare_error_string():
    from_dict = ctl.result_dict({"camera": "A", "ok": True, "packet_id": "pkt_1", "errors": []})
    assert from_dict == {"camera": "A", "ok": True, "packet_id": "pkt_1", "errors": []}
    obj = types.SimpleNamespace(camera="B", ok=False, packet_id=None, errors="boom")
    assert ctl.result_dict(obj) == {"camera": "B", "ok": False, "packet_id": None, "errors": ["boom"]}
    assert ctl.result_dict({"ok": True}, camera="C")["camera"] == "C"
    assert ctl.result_dict({}) == {"camera": "", "ok": False, "packet_id": None, "errors": []}


def test_fake_controller_recipes_merge_and_stay_per_camera():
    fake = ctl.FakeController()
    fake.set_recipe("CAM_main", {"engine_id": "gpt-image-2.5", "preset": "night-relight"})
    assert fake.recipe("CAM_main")["engine_id"] == "gpt-image-2.5"
    assert fake.recipe("CAM_main")["camera"] == "CAM_main"
    assert fake.recipe("CAM_lobby")["engine_id"] == ctl.ENGINE_IDS[0]
    fake.set_recipe("CAM_main", {"camera": "someone else"})
    assert fake.recipe("CAM_main")["camera"] == "CAM_main"  # the key wins over the payload
    got = fake.recipe("CAM_main")
    got["engine_id"] = "mutated"
    assert fake.recipe("CAM_main")["engine_id"] == "gpt-image-2.5"  # recipe() hands out a copy


class TestLiveController:
    def test_cameras_come_from_the_open_scene_sorted_by_name(self, clubhouse):
        cams = ctl.LiveController().cameras()
        assert [c["name"] for c in cams] == ["CAM_aerial", "CAM_entry", "CAM_front"]
        assert all(c["hidden"] is False for c in cams)
        assert all(c["class"] for c in cams)

    def test_a_hidden_camera_is_flagged(self, clubhouse):
        clubhouse.cameras["CAM_entry"].isHidden = True
        by_name = {c["name"]: c for c in ctl.LiveController().cameras()}
        assert by_name["CAM_entry"]["hidden"] is True and by_name["CAM_front"]["hidden"] is False

    def test_lint_reports_the_scene_checks_as_data(self, clubhouse):
        report = ctl.LiveController().lint("CAM_front")
        assert report["passed"] is True and report["problems"] == []
        clubhouse.cameras["CAM_front"].isHidden = True
        again = ctl.LiveController().lint("CAM_front")
        assert again["passed"] is False
        assert [p["code"] for p in again["problems"]] == [policy.HIDDEN_CAMERA]

    def test_lint_surfaces_a_scaled_camera(self, install_scene):
        install_scene("scaled_camera")
        report = ctl.LiveController().lint("CAM_scaled")
        assert not report["passed"]
        assert policy.SCALED_CAMERA in [p["code"] for p in report["problems"]]

    def test_lint_of_an_unknown_camera_is_a_failed_report_not_an_exception(self, clubhouse):
        report = ctl.LiveController().lint("CAM_nope")
        assert report["passed"] is False
        assert report["problems"][0]["code"] == "LINT_ERROR"
        assert report["problems"][0]["node"] == "CAM_nope"

    def test_capabilities_list_only_the_action_modules_that_exist(self, monkeypatch):
        live = ctl.LiveController()
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: False))
        assert live.capabilities() == set()
        monkeypatch.setattr(
            ctl.LiveController, "_module_exists", staticmethod(lambda name: name.endswith("send_packet"))
        )
        assert live.capabilities() == {ctl.CAP_SEND_PACKET}
        # Motion, Light and Scout are never reported until their actions are wired (T37/T38)
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: True))
        caps = live.capabilities()
        assert caps == {ctl.CAP_SEND_PACKET, ctl.CAP_BATCH, ctl.CAP_RECEIVE}
        assert not caps & {ctl.CAP_MOTION, ctl.CAP_LIGHT, ctl.CAP_SCOUT}

    def test_an_unavailable_action_returns_a_failed_result_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: False))
        live = ctl.LiveController()
        results = live.send_packet(["CAM_x"], lambda *a: None)
        assert results[0]["ok"] is False and "not available" in results[0]["errors"][0]
        assert live.receive("x.png", "vfb_history") == (False, "Receive is not available in this build")

    def test_actions_get_the_documented_arguments(self, monkeypatch, tmp_path):
        seen = {}

        def send_run(cameras, out_root, daemon_base, options):
            seen["send"] = (cameras, out_root, daemon_base, options)
            return [{"camera": cameras[0], "ok": True, "packet_id": "pkt_1", "errors": []}]

        def batch_run(recipes, out_root, daemon_base, options):
            seen["batch"] = (recipes, out_root, daemon_base, options)
            return [
                types.SimpleNamespace(camera=r["camera"], ok=True, packet_id="pkt_2", errors=[])
                for r in recipes
            ]

        def load_result(path, mode):
            seen["receive"] = (path, mode)
            return True, "loaded"

        modules = {
            "send_packet": types.SimpleNamespace(run=send_run),
            "batch_cameras": types.SimpleNamespace(run=batch_run),
            "receive": types.SimpleNamespace(load_result=load_result),
        }
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: True))
        monkeypatch.setattr(ctl.LiveController, "_action", lambda self, cap: modules[cap])
        monkeypatch.setattr(
            ctl.LiveController, "_RECIPES_MODULE", "plate_max.no_such_recipes"
        )  # dict recipes
        packets = tmp_path / "packets"
        (tmp_path / "plate-config").mkdir(exist_ok=True)
        (tmp_path / "plate-config" / "config.json").write_text(
            json.dumps({"daemon_url": "http://127.0.0.1:4999", "packets_dir": str(packets)}),
            encoding="utf-8",
            newline="\n",
        )
        live = ctl.LiveController()
        live.set_recipe("CAM_a", {"engine_id": "gpt-image-2.5"})
        progress = lambda done, total, message: None  # noqa: E731

        sent = live.send_packet(["CAM_a"], progress)
        assert sent == [{"camera": "CAM_a", "ok": True, "packet_id": "pkt_1", "errors": []}]
        cameras, out_root, base, options = seen["send"]
        assert cameras == ["CAM_a"] and out_root == packets and base == "http://127.0.0.1:4999"
        assert options["progress"] is progress and options["recipes"]["CAM_a"]["engine_id"] == "gpt-image-2.5"
        # the actions are written separately: they may read options as a dict or as attributes
        assert (
            options.progress is progress and options.backup is False and options.recipes is options["recipes"]
        )
        assert options.something_else is None
        with pytest.raises(AttributeError):
            options.__wrapped__  # noqa: B018 dunder lookups are not swallowed

        batched = live.batch_cameras(["CAM_a", "CAM_b"], progress)
        recipes = seen["batch"][0]
        assert [r["camera"] for r in recipes] == ["CAM_a", "CAM_b"]
        assert recipes[0]["engine_id"] == "gpt-image-2.5" and recipes[1]["engine_id"] == ctl.ENGINE_IDS[0]
        assert [r["packet_id"] for r in batched] == ["pkt_2", "pkt_2"]

        assert live.receive("r.png", "viewport_background") == (True, "loaded")
        assert seen["receive"] == ("r.png", "viewport_background")

    def test_recipes_reach_the_actions_as_their_own_camera_recipe_type_when_it_exists(self, monkeypatch):
        import dataclasses

        @dataclasses.dataclass
        class CameraRecipe:
            camera: str
            engine_id: str
            preset: str
            safe_frame: dict | None
            overscan_aspect: str | None
            editable_classes: list
            extra_elements: list

        recipes_module = types.ModuleType("plate_max.recipes")
        recipes_module.CameraRecipe = CameraRecipe
        monkeypatch.setitem(sys.modules, "plate_max.recipes", recipes_module)
        seen = {}

        def batch_run(recipes, out_root, daemon_base, options):
            seen["recipes"] = recipes
            seen["options"] = options
            return [{"camera": r.camera, "ok": True, "packet_id": "pkt_3", "errors": []} for r in recipes]

        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: True))
        monkeypatch.setattr(
            ctl.LiveController, "_action", lambda self, cap: types.SimpleNamespace(run=batch_run)
        )
        live = ctl.LiveController()
        live.set_recipe("CAM_a", {"preset": "night-relight"})
        out = live.batch_cameras(["CAM_a"], lambda *a: None)
        assert out[0]["ok"] and isinstance(seen["recipes"][0], CameraRecipe)
        assert seen["recipes"][0].preset == "night-relight"
        assert isinstance(seen["options"].recipes["CAM_a"], CameraRecipe)

    def test_a_recipe_type_that_rejects_the_fields_falls_back_to_the_dict(self, monkeypatch):
        recipes_module = types.ModuleType("plate_max.recipes")
        recipes_module.CameraRecipe = lambda **fields: 1 / 0
        monkeypatch.setitem(sys.modules, "plate_max.recipes", recipes_module)
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: True))
        rec = ctl.LiveController()._recipe_object("CAM_a")
        assert isinstance(rec, dict) and rec["camera"] == "CAM_a"

    @pytest.mark.parametrize("method", ["send_motion", "light"])
    def test_unwired_camera_actions_refuse_loudly(self, method):
        with pytest.raises(NotImplementedError):
            getattr(ctl.LiveController(), method)("CAM_x", lambda *a: None)

    def test_unwired_scout_refuses_loudly(self):
        with pytest.raises(NotImplementedError):
            ctl.LiveController().scout(lambda *a: None)


@pytest.fixture
def stub_daemon(tmp_path):
    """A loopback http.server that answers /api/v0/health with whatever health holds; the config.json
    that conftest points PLATE_CONFIG at is told to use it."""
    state = {"health": {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            raw = json.dumps(state["health"]).encode("utf-8")
            self.send_response(200 if self.path == "/api/v0/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    config = Path(os.environ["PLATE_CONFIG"])
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"daemon_url": url}), encoding="utf-8", newline="\n")
    state["url"] = url
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestDaemonStatus:
    def test_a_matching_daemon_is_compatible(self, stub_daemon):
        status = ctl.LiveController().daemon_status()
        assert status["reachable"] and status["ok"] and status["reason"] == ""
        assert status["daemon_version"] == PLUGIN_VERSION and status["url"] == stub_daemon["url"]

    def test_a_daemon_that_does_not_read_our_packet_version_is_reachable_but_not_ok(self, stub_daemon):
        stub_daemon["health"] = {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.2"]}
        status = ctl.LiveController().daemon_status()
        assert status["reachable"] is True and status["ok"] is False
        assert PACKET_VERSION.rsplit(".", 1)[0] in status["reason"]

    def test_a_different_release_is_not_compatible(self, stub_daemon):
        stub_daemon["health"] = {"ok": True, "daemon_version": "9.9.0", "packet_versions": ["0.1"]}
        status = ctl.LiveController().daemon_status()
        assert status["ok"] is False and "9.9.0" in status["reason"]

    def test_no_daemon_is_unreachable_data_not_an_exception(self, tmp_path):
        config = Path(os.environ["PLATE_CONFIG"])
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"daemon_url": "http://127.0.0.1:9"}), encoding="utf-8", newline="\n")
        status = ctl.LiveController().daemon_status()
        assert status["reachable"] is False and status["ok"] is False and status["reason"]


class TestBootstrapLaunch:
    """bootstrap.launch() without Qt: the failure path and the re-entrancy guard."""

    @pytest.fixture(autouse=True)
    def _fresh(self):
        bootstrap.reset()
        yield
        bootstrap.reset()

    def test_a_failed_launch_is_reported_and_never_raised(self, monkeypatch, capsys):
        shown = []
        fake_rt = types.SimpleNamespace(messageBox=lambda message, title="": shown.append((message, title)))
        monkeypatch.setattr(bootstrap, "runtime", lambda: fake_rt)
        broken = types.ModuleType("plate_max.ui.dock")  # no show_dock: AttributeError inside launch()
        monkeypatch.setitem(sys.modules, "plate_max.ui.dock", broken)
        monkeypatch.setattr(sys.modules["plate_max.ui"], "dock", broken, raising=False)
        assert bootstrap.launch() is None
        assert shown and "PLATE failed to launch" in shown[0][0] and shown[0][1] == "PLATE"
        assert "PLATE failed to launch" in capsys.readouterr().out
        assert bootstrap._state["launching"] is False  # the guard is released, a retry is possible

    def test_a_second_launch_while_the_first_is_opening_returns_the_dock_in_progress(self):
        bootstrap._state["launching"] = True
        bootstrap._state["dock"] = "the dock being built"
        assert bootstrap.launch() == "the dock being built"


# ===================================================================== dock half (Qt, offscreen)
@pytest.fixture(scope="module")
def qt():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return types.SimpleNamespace(app=app, QtCore=QtCore, QtWidgets=QtWidgets)


@pytest.fixture
def dockmod(qt):
    from plate_max.ui import dock

    dock.close_dock()
    yield dock
    dock.close_dock()


@pytest.fixture
def make_dock(qt, dockmod):
    """make_dock(controller=None, **FakeController kwargs) -> (dock, controller); closed on teardown."""
    made = []

    def build(controller=None, **kwargs):
        fake = controller if controller is not None else ctl.FakeController(**kwargs)
        widget = dockmod.PlateDock(fake)
        made.append(widget)
        return widget, fake

    yield build
    for widget in made:
        widget.close()
        widget.deleteLater()


USER_ROLE = 256  # Qt.UserRole: the dock keeps the bare camera name there, the label may say "(hidden)"


def select(dock, *names):
    """Select exactly these cameras and focus the first one."""
    for i in range(dock.cam_list.count()):
        if dock.cam_list.item(i).data(USER_ROLE) == names[0]:
            dock.cam_list.setCurrentRow(i)  # replaces the selection
            break
    for i in range(dock.cam_list.count()):
        item = dock.cam_list.item(i)
        item.setSelected(item.data(USER_ROLE) in names)


def calls(fake, name):
    return [c for c in fake.calls if c[0] == name]


def test_the_dock_has_the_documented_controls(make_dock, qt):
    dock, fake = make_dock()
    QtWidgets = qt.QtWidgets
    assert dock.cam_list.selectionMode() == QtWidgets.QAbstractItemView.ExtendedSelection
    assert dock.cam_list.count() == 3
    labels = {
        dock.btn_send: "Send Packet",
        dock.btn_batch: "Batch Cameras",
        dock.btn_motion: "Send Motion",
        dock.btn_receive: "Receive",
        dock.btn_light: "Light",
        dock.btn_scout: "Scout",
    }
    for button, text in labels.items():
        assert button.text() == text
    assert [dock.cb_engine.itemText(i) for i in range(dock.cb_engine.count())] == fake.engines()
    assert dock.cb_overscan.count() == 1 + len(fake.overscan_aspects())
    assert set(dock.class_checks) == set(fake.classes())
    assert set(dock.sp_safe) == {"x", "y", "w", "h"}
    assert dock.lbl_status.text().startswith("daemon 0.1.0")
    assert dock.windowTitle() == "PLATE"


def test_the_style_is_the_maxgaffer_house_palette(dockmod):
    assert dockmod.BG == "#1c1c1e" and dockmod.ACCENT == "#0a84ff"
    assert dockmod.BG in dockmod.STYLE and dockmod.ACCENT in dockmod.STYLE
    assert "gradient" not in dockmod.STYLE.lower() and "shadow" not in dockmod.STYLE.lower()


def test_no_recipe_is_written_back_while_the_dock_only_loads(make_dock):
    dock, fake = make_dock()
    dock.cam_list.setCurrentRow(1)
    dock.cam_list.setCurrentRow(2)
    dock.refresh_cameras()
    assert calls(fake, "set_recipe") == []


def test_recipe_edits_reach_the_controller_for_the_focused_camera(make_dock):
    dock, fake = make_dock()
    dock.cam_list.setCurrentRow(1)  # CAM_terrace
    assert dock.current_camera() == "CAM_terrace"
    dock.cb_engine.setCurrentText("gpt-image-2.5")
    dock.cb_preset.setEditText("night-relight")
    dock.chk_safe.setChecked(True)
    for key, value in {"x": 0, "y": 270, "w": 3840, "h": 1620}.items():
        dock.sp_safe[key].setValue(value)
    dock.cb_overscan.setCurrentIndex(dock.cb_overscan.findData("16:9"))
    for cls, box in dock.class_checks.items():
        box.setChecked(cls == "water")
    rec = fake.recipe("CAM_terrace")
    assert rec["camera"] == "CAM_terrace"
    assert rec["engine_id"] == "gpt-image-2.5" and rec["preset"] == "night-relight"
    assert rec["safe_frame"] == {"x": 0, "y": 270, "w": 3840, "h": 1620}
    assert rec["overscan_aspect"] == "16:9"
    assert rec["editable_classes"] == ["water"]
    # the other cameras keep their own recipe
    assert fake.recipe("CAM_main")["engine_id"] == ctl.ENGINE_IDS[0]
    assert fake.recipe("CAM_main")["safe_frame"] is None


def test_switching_camera_loads_that_cameras_recipe(make_dock):
    dock, fake = make_dock()
    fake.set_recipe(
        "CAM_lobby",
        {
            "engine_id": "seedream-5-pro",
            "preset": "crop-pair-pushin",
            "overscan_aspect": "3:2",
            "safe_frame": {"x": 1, "y": 2, "w": 3, "h": 4},
            "editable_classes": ["sky"],
        },
    )
    fake.calls.clear()
    dock.cam_list.setCurrentRow(2)  # CAM_lobby
    assert dock.cb_engine.currentText() == "seedream-5-pro"
    assert dock.cb_preset.currentText() == "crop-pair-pushin"
    assert dock.cb_overscan.currentData() == "3:2"
    assert dock.chk_safe.isChecked() and dock.sp_safe["h"].value() == 4 and dock.sp_safe["h"].isEnabled()
    assert [c for c, box in dock.class_checks.items() if box.isChecked()] == ["sky"]
    assert "CAM_lobby" in dock.lbl_recipe.text()
    dock.cam_list.setCurrentRow(0)  # CAM_main: defaults, safe frame off
    assert not dock.chk_safe.isChecked() and not dock.sp_safe["w"].isEnabled()
    assert calls(fake, "set_recipe") == []


def test_a_hidden_camera_is_labelled_in_the_list(make_dock):
    fake = ctl.FakeController(cameras=["CAM_a", "CAM_b"])
    fake._cameras[1]["hidden"] = True
    dock, _ = make_dock(fake)
    assert dock.cam_list.item(0).text() == "CAM_a"
    assert dock.cam_list.item(1).text() == "CAM_b  (hidden)"
    assert dock.cam_list.item(1).data(USER_ROLE) == "CAM_b"  # the label never leaks into the camera name


def test_unwired_motion_light_scout_are_disabled_with_a_tooltip(make_dock):
    dock, _ = make_dock()  # FakeController default: send_packet, batch_cameras, receive
    select(dock, "CAM_main")
    dock._refresh_buttons()
    assert dock.btn_send.isEnabled() and dock.btn_batch.isEnabled() and dock.btn_receive.isEnabled()
    for button in (dock.btn_motion, dock.btn_light, dock.btn_scout):
        assert not button.isEnabled()
        assert "not wired" in button.toolTip().lower()


def test_wired_actions_enable_and_follow_the_selection(make_dock):
    dock, _ = make_dock(capabilities=ALL_CAPS)
    dock.cam_list.clearSelection()
    dock._refresh_buttons()
    assert not dock.btn_send.isEnabled() and not dock.btn_batch.isEnabled()
    assert not dock.btn_motion.isEnabled() and not dock.btn_light.isEnabled()
    assert dock.btn_scout.isEnabled() and dock.btn_receive.isEnabled()  # no camera needed
    select(dock, "CAM_lobby")
    assert all(b.isEnabled() for b in (dock.btn_send, dock.btn_batch, dock.btn_motion, dock.btn_light))


def test_send_checks_the_daemon_then_lints_then_sends_with_progress(make_dock):
    dock, fake = make_dock()
    select(dock, "CAM_main", "CAM_lobby")
    fake.calls.clear()
    dock._on_send()
    order = [c[0] for c in fake.calls]
    assert order.index("daemon_status") < order.index("lint") < order.index("send_packet")
    assert set(calls(fake, "send_packet")[0][1]) == {"CAM_main", "CAM_lobby"}
    text = dock.log.toPlainText()
    assert "sent CAM_main" in text and "(packet 01FAKE000)" in text
    assert dock.bar.maximum() == 1 and dock.bar.value() == 0  # progress bar is back at rest
    assert not dock._busy


def test_a_failing_lint_blocks_send_and_shows_the_codes(make_dock):
    dock, fake = make_dock()
    fake.lint_reports["CAM_main"] = {
        "passed": False,
        "unlabelled_share": 0.081,
        "problems": [
            {"code": "UNLABELLED_SHARE", "node": None, "detail": "unlabelled IDs cover 8.1% of the frame"},
            {"code": "BAD_NAME_CHARS", "node": "tree:01", "detail": "rename it"},
        ],
    }
    select(dock, "CAM_main")
    fake.calls.clear()
    dock._on_send()
    assert calls(fake, "send_packet") == []
    view = dock.lint_view.toPlainText()
    assert "CAM_main FAIL" in view and "UNLABELLED_SHARE" in view and "BAD_NAME_CHARS" in view
    assert "tree:01" in view and "8.1%" in view
    assert "blocked" in dock.lbl_lint.text()
    assert "blocked by lint" in dock.log.toPlainText()
    assert dock.btn_send.isEnabled() and not dock._busy


def test_one_failing_camera_blocks_the_whole_send(make_dock):
    dock, fake = make_dock()
    fake.lint_reports["CAM_lobby"] = {
        "passed": False,
        "unlabelled_share": None,
        "problems": [{"code": "HIDDEN_CAMERA", "node": "CAM_lobby", "detail": "unhide it"}],
    }
    select(dock, "CAM_main", "CAM_lobby")
    fake.calls.clear()
    dock._on_batch()
    assert calls(fake, "batch_cameras") == []
    assert (
        "CAM_main PASS" in dock.lint_view.toPlainText() and "CAM_lobby FAIL" in dock.lint_view.toPlainText()
    )


def test_an_incompatible_daemon_blocks_send_before_lint(make_dock):
    dock, fake = make_dock()
    fake.status = {
        "reachable": True,
        "ok": False,
        "daemon_version": "9.9.0",
        "reason": "different release",
        "url": "u",
    }
    select(dock, "CAM_main")
    fake.calls.clear()
    dock._on_send()
    assert [c[0] for c in fake.calls] == ["daemon_status"]
    assert "NOT compatible: different release" in dock.lbl_status.text()
    assert "Not sending" in dock.log.toPlainText()


def test_an_unreachable_daemon_is_shown_and_blocks_send(make_dock):
    dock, fake = make_dock()
    fake.status = {
        "reachable": False,
        "ok": False,
        "daemon_version": None,
        "reason": "connection refused",
        "url": "u",
    }
    select(dock, "CAM_main")
    fake.calls.clear()
    dock._on_send()
    assert dock.lbl_status.text() == "daemon unreachable: connection refused"
    assert calls(fake, "lint") == [] and calls(fake, "send_packet") == []


def test_a_controller_that_raises_in_daemon_status_cannot_break_the_status_line(make_dock):
    class Raising(ctl.FakeController):
        def daemon_status(self):
            raise OSError("socket on fire")

    dock, _ = make_dock(Raising())
    assert dock.lbl_status.text().startswith("daemon unreachable")
    assert "socket on fire" in dock.lbl_status.text()


def test_send_with_nothing_selected_says_so(make_dock):
    dock, fake = make_dock()
    dock.cam_list.clearSelection()
    fake.calls.clear()
    dock._on_send()
    assert "Select at least one camera" in dock.log.toPlainText()
    assert calls(fake, "send_packet") == [] and calls(fake, "daemon_status") == []


def test_batch_cameras_sends_every_selected_camera(make_dock):
    dock, fake = make_dock()
    select(dock, "CAM_main", "CAM_terrace", "CAM_lobby")
    fake.calls.clear()
    dock._on_batch()
    sent = calls(fake, "batch_cameras")[0][1]
    assert sorted(sent) == ["CAM_lobby", "CAM_main", "CAM_terrace"]
    assert dock.log.toPlainText().count(" sent (packet ") == 3


def test_a_failed_result_shows_the_reason(make_dock):
    dock, fake = make_dock()
    fake.results = [
        {"camera": "CAM_main", "ok": False, "packet_id": None, "errors": ["EXR missing VRayZDepth", "retry"]},
    ]
    select(dock, "CAM_main")
    dock._on_send()
    assert "CAM_main FAILED: EXR missing VRayZDepth; retry" in dock.log.toPlainText()


def test_an_action_that_raises_is_logged_and_the_dock_recovers(make_dock):
    dock, fake = make_dock()
    fake.raise_on = "send_packet"
    select(dock, "CAM_main")
    dock._on_send()  # must not raise into Max
    assert "Send Packet failed: RuntimeError: send_packet blew up" in dock.log.toPlainText()
    assert not dock._busy and dock.btn_send.isEnabled() and dock.cam_list.isEnabled()
    fake.raise_on = None
    dock._on_send()
    assert "sent CAM_main" in dock.log.toPlainText()


def test_buttons_are_disabled_while_an_action_runs(make_dock):
    dock, fake = make_dock()
    seen = {}

    def during(name):
        seen["busy"] = dock._busy
        seen["send"] = dock.btn_send.isEnabled()
        seen["batch"] = dock.btn_batch.isEnabled()
        seen["list"] = dock.cam_list.isEnabled()
        dock._on_send()  # a second click while busy is ignored, not queued

    fake.on_action = during
    select(dock, "CAM_main")
    fake.calls.clear()
    dock._on_send()
    assert seen == {"busy": True, "send": False, "batch": False, "list": False}
    assert len(calls(fake, "send_packet")) == 1
    assert dock.btn_send.isEnabled() and dock.cam_list.isEnabled()


def test_receive_passes_the_picked_path_and_mode(make_dock, monkeypatch):
    dock, fake = make_dock()
    monkeypatch.setattr(dock, "_pick_receive_path", lambda: "result.png")
    dock.cb_receive.setCurrentIndex(dock.cb_receive.findData("viewport_background"))
    dock.btn_receive.click()
    assert calls(fake, "receive") == [("receive", "result.png", "viewport_background")]
    assert "Receive: loaded into the VFB history" in dock.log.toPlainText()


def test_receive_cancelled_does_nothing_and_a_refusal_is_shown(make_dock, monkeypatch):
    dock, fake = make_dock()
    monkeypatch.setattr(dock, "_pick_receive_path", lambda: "")
    dock.btn_receive.click()
    assert calls(fake, "receive") == []
    monkeypatch.setattr(dock, "_pick_receive_path", lambda: "bad.exr")
    fake.receive_result = (False, "not a PNG/EXR the VFB can load")
    dock.btn_receive.click()
    assert "Receive: not a PNG/EXR the VFB can load" in dock.log.toPlainText()


def test_motion_light_and_scout_call_the_controller_when_wired(make_dock):
    dock, fake = make_dock(capabilities=ALL_CAPS)
    select(dock, "CAM_lobby")
    fake.calls.clear()
    dock.btn_motion.click()
    dock.btn_light.click()
    dock.btn_scout.click()
    assert [c for c in fake.calls if c[0] in {"send_motion", "light", "scout"}] == [
        ("send_motion", "CAM_lobby"),
        ("light", "CAM_lobby"),
        ("scout",),
    ]


def test_run_lint_with_no_selection_returns_false(make_dock):
    dock, _ = make_dock()
    dock.cam_list.clearSelection()
    assert dock.run_lint() is False
    assert dock.lbl_lint.text() == "select a camera"


def test_lint_output_is_html_escaped(make_dock):
    dock, fake = make_dock()
    fake.lint_reports["CAM_main"] = {
        "passed": False,
        "unlabelled_share": None,
        "problems": [{"code": "BAD_NAME_CHARS", "node": "<b>x</b>", "detail": "a & b <i>"}],
    }
    select(dock, "CAM_main")
    dock.run_lint()
    view = dock.lint_view.toPlainText()
    assert "<b>x</b>" in view and "a & b <i>" in view  # shown literally, not interpreted as markup


def test_daemon_io_runs_off_the_main_thread_but_scene_calls_stay_on_it(make_dock):
    seen = {}

    class Threaded(ctl.FakeController):
        def daemon_status(self):
            seen["status"] = threading.get_ident()
            return super().daemon_status()

        def lint(self, camera):
            seen["lint"] = threading.get_ident()
            return super().lint(camera)

        def cameras(self):
            seen["cameras"] = threading.get_ident()
            return super().cameras()

        def send_packet(self, cameras, progress):
            seen["send"] = threading.get_ident()
            return super().send_packet(cameras, progress)

    main = threading.get_ident()
    dock, _ = make_dock(Threaded())
    select(dock, "CAM_main")
    dock._on_send()
    assert seen["status"] != main, "the daemon health call must not block Max's main thread"
    assert seen["cameras"] == main and seen["lint"] == main and seen["send"] == main, "pymxs stays on main"


def test_run_blocking_io_returns_values_and_reraises_errors(make_dock):
    dock, _ = make_dock()
    assert [dock._run_blocking_io(lambda n=n: n * 2) for n in range(40)] == [n * 2 for n in range(40)]
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        dock._run_blocking_io(lambda: 1 / 0)
    assert dock._workers == []  # every worker thread was joined and dropped


def test_progress_callback_drives_the_bar_and_the_log(make_dock):
    dock, _ = make_dock()
    dock._progress(2, 4, "two of four")
    assert dock.bar.maximum() == 4 and dock.bar.value() == 2
    assert "two of four" in dock.log.toPlainText()
    dock._progress(0, 0, "")  # unknown total: busy indicator
    assert dock.bar.maximum() == 0


def test_show_dock_is_a_singleton_and_close_dock_resets_it(dockmod, qt):
    fake = ctl.FakeController()
    first = dockmod.show_dock(fake)
    try:
        assert isinstance(first, dockmod.PlateDock) and first.isVisible()
        assert dockmod.show_dock(fake) is first
        dockmod.close_dock()
        second = dockmod.show_dock(fake)
        assert second is not first
    finally:
        dockmod.close_dock()


def test_bootstrap_launch_opens_one_dock_and_reuses_it(dockmod, qt):
    bootstrap.reset()
    fake = ctl.FakeController()
    try:
        first = bootstrap.launch(fake)
        assert isinstance(first, dockmod.PlateDock)
        assert bootstrap.launch(fake) is first
    finally:
        bootstrap.reset()
        dockmod.close_dock()


def test_the_dock_paints_a_real_image(make_dock, qt):
    dock, _ = make_dock()
    dock.resize(600, 1000)
    dock.show()
    qt.app.processEvents()
    image = dock.grab().toImage()
    assert image.width() >= 600 and image.height() >= 1000
    seen = set()
    for y in range(0, image.height(), 9):
        for x in range(0, image.width(), 9):
            seen.add(image.pixel(x, y))
    assert len(seen) > 12, "the dock rendered as a flat fill"
    corner = image.pixelColor(2, 2)
    assert (corner.red(), corner.green(), corner.blue()) == (0x1C, 0x1C, 0x1E)  # BG #1c1c1e


def _load_ui_preview():
    path = PLUGIN / "scripts" / "ui_preview.py"
    spec = importlib.util.spec_from_file_location("plate_ui_preview", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ui_preview_renders_the_dock_to_a_png(qt, tmp_path):
    preview = _load_ui_preview()
    out = preview.render(tmp_path / "nested" / "dock.png")
    raw = out.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    assert (width, height) == preview.SIZE
    assert len(raw) > 20_000, "a populated dock compresses to more than a flat fill"
