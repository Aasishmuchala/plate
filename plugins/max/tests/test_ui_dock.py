"""The PLATE dock driven for real, offscreen, against a FakeController.

Two halves. The controller half (LiveController, the recipe defaults, the project binding, the
FakeController itself) needs no Qt and always runs. The dock half builds the real PySide6 widgets with
QT_QPA_PLATFORM=offscreen and clicks through every user journey (send, batch, receive, lint block,
recovery from a failing action, Send Motion / Light / Scout, the Project combo, the canvas-job poller's
lifecycle, the shared-dock launch); it skips cleanly when the PySide6 wheel is not installed (the `qt`
fixture). The daemon is a loopback http.server (health and projects). What this proves that an import
smoke cannot: the buttons reach the controller and the actions with the right arguments, the busy
lifecycle restores the buttons, daemon I/O leaves the main thread while pymxs-facing calls stay on it,
and the dock paints a non-trivial image. What it cannot prove: how it looks inside 3ds Max's Qt, and a
real Send Motion or Light run (verify-on-box).
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
from plate_max import config as plate_config  # noqa: E402
from plate_max.packet_id import new_packet_id  # noqa: E402
from plate_max.ui import controller as ctl  # noqa: E402

TOKEN = "c0ffeec0ffeec0ffeec0ffeec0ffeec0"

PLUGIN = Path(ctl.__file__).resolve().parent.parent.parent
ALL_CAPS = (
    ctl.CAP_SEND_PACKET,
    ctl.CAP_BATCH,
    ctl.CAP_RECEIVE,
    ctl.CAP_MOTION,
    ctl.CAP_LIGHT,
    ctl.CAP_SCOUT,
)


@pytest.fixture
def no_gaffer(monkeypatch):
    """No MaxGaffer in this Max: import maxgaffer raises ImportError."""
    monkeypatch.setitem(sys.modules, "maxgaffer", None)
    monkeypatch.setitem(sys.modules, "maxgaffer.api", None)


@pytest.fixture
def gaffer_api(monkeypatch):
    """A maxgaffer.api with the two calls actions.light needs (never called by these tests)."""
    pkg = types.ModuleType("maxgaffer")
    api = types.ModuleType("maxgaffer.api")
    api.match_camera = lambda *a, **k: {"score": 1.0}
    api.apply_camera_state = lambda camera: []
    pkg.api = api
    monkeypatch.setitem(sys.modules, "maxgaffer", pkg)
    monkeypatch.setitem(sys.modules, "maxgaffer.api", api)
    return api


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


def test_result_dict_passes_the_send_answer_through():
    from plate_max.actions import light, send_packet

    sent = send_packet.SendResult(
        camera="A",
        ok=True,
        packet_id="pkt_9",
        status_code=202,
        events_url="/api/v0/packets/pkt_9/events",
        ingest="ingesting",
        warnings=["dome swapped"],
    )
    out = ctl.result_dict(sent)
    assert (
        out["status_code"] == 202 and out["events_url"].endswith("/events") and out["ingest"] == "ingesting"
    )
    assert out["warnings"] == ["dome swapped"] and "preview" not in out and "code" not in out
    relit = light.LightResult(
        camera="A", ok=True, packet_id="pkt_10", send={"status_code": 200, "ingest": None}
    )
    assert ctl.result_dict(relit)["status_code"] == 200  # Light: read from its re-render


@pytest.mark.parametrize(
    ("result", "line", "err"),
    [
        (
            {"camera": "A", "ok": True, "packet_id": "p1", "status_code": 202},
            "S: A: Sent, ingesting (packet p1)",
            False,
        ),
        (
            {"camera": "A", "ok": True, "packet_id": "p1", "status_code": 202, "ingest": "ingested"},
            "S: A: Sent, ingested (packet p1)",
            False,
        ),
        (
            {"camera": "A", "ok": True, "packet_id": "p1", "status_code": 200},
            "S: A: Already sent (packet p1)",
            False,
        ),
        (
            {
                "camera": "A",
                "ok": False,
                "packet_id": "p1",
                "status_code": 202,
                "ingest": "failed: bad EXR; no Z",
            },
            "S: A: Ingest failed: bad EXR; no Z",
            True,
        ),
        ({"camera": "A", "ok": True, "packet_id": "p1"}, "S: A sent (packet p1)", False),
        ({"camera": "A", "ok": False, "errors": ["x", "y"]}, "S: A FAILED: x; y", True),
        (
            {"camera": "__PLATE_scout_0", "ok": True, "preview": "/s/0.png"},
            "S: __PLATE_scout_0 preview /s/0.png",
            False,
        ),
    ],
)
def test_result_message_says_what_the_daemon_answered(result, line, err):
    assert ctl.result_message("S", result) == (line, err)


def test_custody_words_and_the_packet_floor():
    assert ctl.custody_text(None) == "custody unset"
    assert ctl.custody_text({"nda": False, "allowed_grades": ["C0"], "unset": True}) == "custody unset"
    assert ctl.custody_text({"nda": True, "allowed_grades": ["C0", "C1"]}) == "NDA · C0, C1"
    assert ctl.custody_text({"nda": False, "allowed_grades": ["C0", "C1", "C2", "D"]}) == "C0, C1, C2, D"
    assert ctl.packet_custody({"nda": True, "allowed_grades": ["C0"], "root": "x"}) == {
        "nda": True,
        "allowed_grades": ["C0"],
    }
    assert ctl.packet_custody({"nda": False, "allowed_grades": ["C0"], "unset": True}) is None
    assert ctl.packet_custody(None) is None


# ---- the project binding: <scene>.plate.json, then config.json's project_id, then the stem
def _save_scene(runtime, folder: Path, name: str = "Club House_v2.max") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    runtime.maxFilePath, runtime.maxFileName = str(folder), name
    return folder / name


def _write_config(**keys) -> Path:
    path = Path(os.environ["PLATE_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8", newline="\n")
    return path


def test_project_binding_order_is_sidecar_then_config_then_stem(scene, tmp_path):
    live = ctl.LiveController()
    assert live.project_id() == "untitled"  # an unsaved scene with no config key
    max_file = _save_scene(scene, tmp_path / "jobs")
    live = ctl.LiveController()
    assert live.project_id() == "club-house-v2"  # send_packet's own default: the stem slug
    _write_config(project_id="tula_wot")
    assert ctl.LiveController().project_id() == "tula_wot"  # a schema slug keeps its underscore
    sidecar = max_file.with_name("Club House_v2.plate.json")
    sidecar.write_text(json.dumps({"project_id": "abhigna", "cameras": {}}), encoding="utf-8", newline="\n")
    assert ctl.LiveController().project_id() == "abhigna"
    assert plate_config.project_id() == "tula_wot" and plate_config.CONFIG_KEYS[-1] == "packets_dir"


def test_set_project_persists_in_the_scene_sidecar_and_keeps_the_recipes(scene, tmp_path):
    from plate_max import recipes

    max_file = _save_scene(scene, tmp_path / "jobs", "lobby.max")
    recipes.set_recipe(recipes.CameraRecipe(camera="CAM_a", preset="night-relight"), max_file)
    live = ctl.LiveController()
    saved = live.set_project("Tula WoT")
    assert saved == max_file.with_name("lobby.plate.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["project_id"] == "tula-wot" and data["cameras"]["CAM_a"]["preset"] == "night-relight"
    assert live.project_id() == "tula-wot" and ctl.LiveController().project_id() == "tula-wot"
    recipes.set_recipe(recipes.CameraRecipe(camera="CAM_b"), max_file)  # a recipe save keeps the binding
    assert json.loads(saved.read_text(encoding="utf-8"))["project_id"] == "tula-wot"
    with pytest.raises(ValueError):
        live.set_project("  !!  ")
    scene.maxFileName = ""  # another, unsaved scene: nowhere to write, the binding is the stem default
    assert ctl.LiveController().set_project("adhoc") is None


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

    def test_capabilities_list_only_the_action_modules_that_exist(self, monkeypatch, no_gaffer):
        live = ctl.LiveController()
        monkeypatch.setattr(ctl.LiveController, "_module_exists", staticmethod(lambda name: False))
        assert live.capabilities() == set()
        assert set(live.unavailable()) == set(ALL_CAPS)
        monkeypatch.setattr(
            ctl.LiveController, "_module_exists", staticmethod(lambda name: name.endswith("send_packet"))
        )
        assert live.capabilities() == {ctl.CAP_SEND_PACKET}

    def test_motion_and_scout_enable_when_their_modules_import_and_light_needs_maxgaffer(self, no_gaffer):
        live = ctl.LiveController()  # the real plate_max.actions modules
        caps = live.capabilities()
        assert caps == set(ALL_CAPS) - {ctl.CAP_LIGHT}
        why = live.unavailable()
        assert list(why) == [ctl.CAP_LIGHT] and why[ctl.CAP_LIGHT].startswith("MAXGAFFER_MISSING")
        [refused] = live.light("CAM_x", lambda *a: None, reference_path="ref.png")
        assert refused["ok"] is False and refused["code"] == "MAXGAFFER_MISSING"

    def test_light_enables_with_maxgaffer_installed(self, gaffer_api):
        live = ctl.LiveController()
        assert live.capabilities() == set(ALL_CAPS) and live.unavailable() == {}

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

    def test_send_motion_reaches_the_action_with_the_hero_the_range_and_the_project(
        self, clubhouse, monkeypatch, tmp_path
    ):
        packets = tmp_path / "packets"
        _write_config(token=TOKEN, daemon_url="http://127.0.0.1:4999", packets_dir=str(packets))
        clubhouse.animationRange = types.SimpleNamespace(
            start=types.SimpleNamespace(frame=10), end=types.SimpleNamespace(frame=34)
        )
        seen = {}

        def motion_run(
            camera, hero_packet_id, f0, f1, out_root, daemon_base=None, crossings=(), options=None
        ):
            seen.update(camera=camera, hero=hero_packet_id, f0=f0, f1=f1, out_root=out_root, base=daemon_base)
            seen.update(crossings=crossings, options=options)
            return types.SimpleNamespace(
                camera=camera, ok=True, packet_id="pkt_motion", errors=[], status_code=202
            )

        modules = {ctl.CAP_MOTION: types.SimpleNamespace(run=motion_run)}
        monkeypatch.setattr(ctl.LiveController, "_action", lambda self, cap: modules[cap])
        live = ctl.LiveController()
        [no_hero] = live.send_motion("CAM_front", lambda *a: None)
        assert no_hero["ok"] is False and "Send Packet first" in no_hero["errors"][0] and seen == {}
        live.note_packet("CAM_front", "pkt_hero")
        live.set_project("tula-wot")
        live._projects = {
            "tula-wot": {"project_id": "tula-wot", "custody": {"nda": True, "allowed_grades": ["C0"]}}
        }
        [sent] = live.send_motion("CAM_front", lambda *a: None)
        assert sent == {
            "camera": "CAM_front",
            "ok": True,
            "packet_id": "pkt_motion",
            "errors": [],
            "status_code": 202,
        }
        assert (seen["camera"], seen["hero"], seen["f0"], seen["f1"]) == ("CAM_front", "pkt_hero", 10, 34)
        assert (
            seen["out_root"] == packets
            and seen["base"] == "http://127.0.0.1:4999"
            and seen["crossings"] == []
        )
        opts = seen["options"]
        assert opts.project_id == "tula-wot" and opts.custody == {"nda": True, "allowed_grades": ["C0"]}
        assert "supersedes" not in opts  # "supersedes last" is not ticked
        live.set_supersede_last(True)
        live.send_motion("CAM_front", lambda *a: None, hero_packet_id="pkt_other")
        assert (
            seen["hero"] == "pkt_other" and seen["options"]["supersedes"] == "pkt_motion"
        )  # one id for a move

    def test_light_runs_match_apply_and_rerender_through_the_light_action(self, gaffer_api, monkeypatch):
        seen = {}

        def light_run(cameras, out_root, daemon_base=None, options=None):
            seen.update(cameras=cameras, options=options)
            return [
                types.SimpleNamespace(camera=cameras[0], ok=True, packet_id="pkt_relit", errors=[], send=None)
            ]

        real = ctl.LiveController._action
        monkeypatch.setattr(
            ctl.LiveController,
            "_action",
            lambda self, cap: (
                types.SimpleNamespace(run=light_run, available=lambda: (True, ""))
                if cap == ctl.CAP_LIGHT
                else real(self, cap)
            ),
        )
        live = ctl.LiveController()
        [no_ref] = live.light("CAM_a", lambda *a: None)
        assert no_ref["ok"] is False and no_ref["code"] == "NO_REFERENCE"
        live.note_packet("CAM_a", "pkt_old")
        [relit] = live.light(
            "CAM_a", lambda *a: None, reference_path="/refs/dusk.jpg", locks=["sun"], deep=True
        )
        assert relit["ok"] and live.last_packet("CAM_a") == "pkt_relit"
        opts = seen["options"]
        assert seen["cameras"] == ["CAM_a"] and opts.reference_path == "/refs/dusk.jpg"
        assert opts.locks == ["sun"] and opts.deep is True and opts.supersedes == {"CAM_a": "pkt_old"}

    def test_scout_runs_the_scout_action_with_keep(self, monkeypatch):
        seen = {}

        def scout_run(out_root, daemon_base=None, options=None):
            seen.update(out_root=out_root, options=options)
            return [
                types.SimpleNamespace(
                    camera="__PLATE_scout_0", ok=True, packet_id=None, errors=[], preview="/s.png"
                )
            ]

        monkeypatch.setattr(
            ctl.LiveController, "_action", lambda self, cap: types.SimpleNamespace(run=scout_run)
        )
        monkeypatch.setattr(ctl.LiveController, "capabilities", lambda self: {ctl.CAP_SCOUT})
        [view] = ctl.LiveController().scout(lambda *a: None, keep=True)
        assert (
            view["preview"] == "/s.png" and seen["options"].keep is True and "packets" not in seen["options"]
        )

    def test_options_carry_the_project_its_custody_and_supersedes_last(self, monkeypatch, scene):
        live = ctl.LiveController()
        live.set_project("abhigna")
        opts = live._options(["CAM_a", "CAM_b"], lambda *a: None)
        assert (
            opts.project_id == "abhigna" and opts.custody is None and opts.supersedes is None
        )  # unknown project
        live._projects = {
            "abhigna": {
                "project_id": "abhigna",
                "custody": {"nda": False, "allowed_grades": ["C0"], "unset": True},
            }
        }
        assert live._options(["CAM_a"], lambda *a: None).custody is None  # unset: no floor in the packet
        live._projects["abhigna"]["custody"] = {"nda": False, "allowed_grades": ["C0", "C1", "C2"]}
        live.note_packet("CAM_a", "pkt_a")
        live.set_supersede_last(True)
        opts = live._options(["CAM_a", "CAM_b"], lambda *a: None)
        assert opts.custody == {"nda": False, "allowed_grades": ["C0", "C1", "C2"]}
        assert opts.supersedes == {"CAM_a": "pkt_a"}  # only the camera whose last packet is known

    def test_job_options_carry_the_jobs_project_and_only_the_edited_recipes(self, scene):
        live = ctl.LiveController()
        live.set_recipe("CAM_a", {"preset": "night-relight"})
        opts = live.job_options(["CAM_a", "CAM_b"], "tula-wot")
        assert opts.project_id == "tula-wot" and set(opts.recipes) == {"CAM_a"}

    def test_hero_candidates_are_the_cameras_stills_in_the_project_newest_first(self, scene, tmp_path):
        packets = tmp_path / "packets"
        _write_config(packets_dir=str(packets))
        ids = sorted(new_packet_id() for _ in range(4))

        def write(pid, camera="CAM_a", kind="still", project="untitled"):
            (packets / pid).mkdir(parents=True)
            manifest = {"packet_id": pid, "kind": kind, "project_id": project, "camera": {"name": camera}}
            (packets / pid / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        write(ids[0])
        write(ids[1], camera="CAM_b")
        write(ids[2], kind="motion")
        write(ids[3], project="elsewhere")
        live = ctl.LiveController()
        assert live.hero_candidates("CAM_a") == [ids[0]]
        live.note_packet("CAM_a", "pkt_session")
        assert live.hero_candidates("CAM_a") == ["pkt_session", ids[0]]

    def test_projects_come_from_get_projects_with_the_bearer(self, stub_daemon):
        live = ctl.LiveController()
        heads = live.projects()
        assert [h["project_id"] for h in heads] == ["clubhouse", "tula-wot"]
        assert stub_daemon["auth"] == [f"Bearer {TOKEN}"]
        assert live.project_custody("tula-wot") == {"nda": True, "allowed_grades": ["C0", "C1"]}
        assert live.project_custody("nobody") is None

    def test_make_poller_builds_the_job_poller_over_this_controller(self):
        from plate_max import jobs

        live = ctl.LiveController()
        poller = live.make_poller(log=print)
        assert isinstance(poller, jobs.Poller) and poller.controller is live and poller.interval_s == 5.0
        assert ctl.FakeController().make_poller() is None  # a dock under test never polls a real daemon


@pytest.fixture
def stub_daemon(tmp_path):
    """A loopback http.server that answers /api/v0/health with whatever health holds; the config.json
    that conftest points PLATE_CONFIG at is told to use it."""
    state = {
        "health": {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]},
        "projects": [
            {"project_id": "clubhouse", "custody": {"nda": False, "allowed_grades": ["C0", "C1", "C2", "D"]}},
            {"project_id": "tula-wot", "custody": {"nda": True, "allowed_grades": ["C0", "C1"]}},
        ],
        "auth": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/v0/projects":
                state["auth"].append(self.headers.get("Authorization"))
                raw = json.dumps(state["projects"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
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
    config.write_text(json.dumps({"daemon_url": url, "token": TOKEN}), encoding="utf-8", newline="\n")
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


def test_motion_light_and_scout_call_the_controller_when_wired(make_dock, monkeypatch):
    dock, fake = make_dock(capabilities=ALL_CAPS)
    select(dock, "CAM_lobby")
    fake.last_packets["CAM_lobby"] = "pkt_hero"  # the dock sent this camera's still earlier
    monkeypatch.setattr(dock, "_pick_reference_path", lambda: "/refs/golden_hour.jpg")
    fake.calls.clear()
    dock.btn_motion.click()
    dock.btn_light.click()
    dock.btn_scout.click()
    dock.chk_scout_keep.setChecked(True)
    dock.btn_scout.click()
    assert [c for c in fake.calls if c[0] in {"send_motion", "light", "scout"}] == [
        ("send_motion", "CAM_lobby", "pkt_hero"),
        ("light", "CAM_lobby", "/refs/golden_hour.jpg"),
        ("scout", False),
        ("scout", True),
    ]
    assert "Send Motion: CAM_lobby from hero still pkt_hero" in dock.log.toPlainText()


def test_light_cancelled_at_the_reference_picker_does_nothing(make_dock, monkeypatch):
    dock, fake = make_dock(capabilities=ALL_CAPS)
    select(dock, "CAM_main")
    monkeypatch.setattr(dock, "_pick_reference_path", lambda: "")
    fake.calls.clear()
    dock.btn_light.click()
    assert calls(fake, "light") == [] and calls(fake, "daemon_status") == []


def test_send_motion_asks_for_the_hero_when_the_dock_has_sent_none(make_dock, monkeypatch, qt):
    dock, fake = make_dock(capabilities=ALL_CAPS)
    select(dock, "CAM_main")
    dock.btn_motion.click()  # no hero anywhere: said, nothing sent
    assert calls(fake, "send_motion") == []
    assert "needs a hero still of CAM_main: Send Packet first" in dock.log.toPlainText()
    fake.heroes["CAM_main"] = ["pkt_new", "pkt_old"]
    asked = []

    def get_item(parent, title, label, items, current, editable):
        asked.append((title, list(items), editable))
        return "pkt_old", True

    monkeypatch.setattr(qt.QtWidgets.QInputDialog, "getItem", staticmethod(get_item))
    dock.btn_motion.click()
    assert asked == [("Hero still", ["pkt_new", "pkt_old"], True)]
    assert calls(fake, "send_motion") == [("send_motion", "CAM_main", "pkt_old")]


def test_the_three_camera_actions_enable_in_a_live_dock_when_their_modules_import(
    qt, dockmod, stub_daemon, clubhouse, gaffer_api, monkeypatch
):
    monkeypatch.setattr(ctl.LiveController, "make_poller", lambda self, **hooks: None)
    dock = dockmod.PlateDock(ctl.LiveController())  # the real plate_max.actions, a fake MaxGaffer
    try:
        select(dock, "CAM_front")
        assert dock.btn_motion.isEnabled() and dock.btn_light.isEnabled() and dock.btn_scout.isEnabled()
        assert dock.btn_light.toolTip() == "" and dock.chk_scout_keep.isEnabled()
    finally:
        dock.close()
        dock.deleteLater()


def test_light_without_maxgaffer_is_disabled_with_the_reason(make_dock):
    fake = ctl.FakeController(capabilities=set(ALL_CAPS) - {ctl.CAP_LIGHT})
    fake.unavailable_reasons = {
        ctl.CAP_LIGHT: "MAXGAFFER_MISSING: MaxGaffer is not installed in this 3ds Max"
    }
    dock, _ = make_dock(fake)
    select(dock, "CAM_main")
    dock._refresh_buttons()
    assert dock.btn_motion.isEnabled() and dock.btn_scout.isEnabled()
    assert not dock.btn_light.isEnabled() and dock.btn_light.toolTip().startswith("MAXGAFFER_MISSING")


def test_engine_and_preset_are_labelled_hints_that_are_not_sent(make_dock, qt):
    dock, _ = make_dock()
    form = dock.cb_engine.parentWidget().layout()
    labels = [
        w.text()
        for w in dock.findChildren(qt.QtWidgets.QLabel)
        if w.buddy() in (dock.cb_engine, dock.cb_preset) or w.text().endswith("(not sent)")
    ]
    assert "Engine hint (not sent)" in labels and "Preset hint (not sent)" in labels, (form, labels)
    assert "not sent" in dock.cb_engine.toolTip() and "Make bar" in dock.cb_preset.toolTip()


# ---- the Project combo
def test_the_project_combo_lists_the_daemon_projects_and_binds_the_scene(make_dock):
    dock, fake = make_dock()
    items = [dock.cb_project.itemText(i) for i in range(dock.cb_project.count())]
    assert items == ["clubhouse", "tula-wot"] and dock.cb_project.currentText() == "clubhouse"
    assert dock.lbl_custody.text() == "C0, C1, C2"
    fake.calls.clear()
    dock.cb_project.setCurrentIndex(dock.cb_project.findData("tula-wot"))
    assert calls(fake, "set_project") == [("set_project", "tula-wot")] and fake.bound_project == "tula-wot"
    assert dock.lbl_custody.text() == "NDA · C0, C1"
    assert "Project: tula-wot (this scene is unsaved" in dock.log.toPlainText()
    dock.cb_project.setEditText("Night Shoot 2")  # a new project typed in: slugged, then bound
    dock.cb_project.lineEdit().editingFinished.emit()
    assert fake.bound_project == "night-shoot-2" and dock.cb_project.currentText() == "night-shoot-2"
    dock.cb_project.setEditText("!!!")
    dock.cb_project.lineEdit().editingFinished.emit()
    assert fake.bound_project == "night-shoot-2" and "not a project id" in dock.log.toPlainText()
    assert dock.cb_project.currentText() == "night-shoot-2"


def test_an_unreachable_daemon_leaves_the_binding_shown(make_dock):
    fake = ctl.FakeController()
    fake.projects_error = OSError("connection refused")
    dock, _ = make_dock(fake)
    assert dock.cb_project.currentText() == "clubhouse" and "connection refused" in dock.cb_project.toolTip()


def test_the_project_combo_persists_to_the_scene_sidecar(
    qt, dockmod, stub_daemon, clubhouse, tmp_path, monkeypatch
):
    monkeypatch.setattr(ctl.LiveController, "make_poller", lambda self, **hooks: None)
    max_file = _save_scene(clubhouse, tmp_path / "jobs", "clubhouse.max")
    dock = dockmod.PlateDock(ctl.LiveController())
    try:
        assert [dock.cb_project.itemText(i) for i in range(dock.cb_project.count())] == [
            "clubhouse",
            "tula-wot",
        ]
        dock.cb_project.setCurrentIndex(dock.cb_project.findData("tula-wot"))
        sidecar = max_file.with_name("clubhouse.plate.json")
        assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] == "tula-wot"
        assert dock.lbl_custody.text() == "NDA · C0, C1"
        again = dockmod.PlateDock(ctl.LiveController())  # the next session reads the binding back
        assert again.cb_project.currentText() == "tula-wot"
        again.close()
        again.deleteLater()
    finally:
        dock.close()
        dock.deleteLater()


def test_send_motion_from_the_dock_reaches_the_action_with_the_project_from_the_combo(
    qt, dockmod, stub_daemon, clubhouse, tmp_path, monkeypatch
):
    monkeypatch.setattr(ctl.LiveController, "make_poller", lambda self, **hooks: None)
    _save_scene(clubhouse, tmp_path / "jobs", "clubhouse.max")
    seen = {}

    def motion_run(camera, hero_packet_id, f0, f1, out_root, daemon_base=None, crossings=(), options=None):
        seen.update(camera=camera, hero=hero_packet_id, options=options, base=daemon_base)
        return {"camera": camera, "ok": True, "packet_id": "pkt_move", "errors": [], "status_code": 202}

    real_action = ctl.LiveController._action
    monkeypatch.setattr(
        ctl.LiveController,
        "_action",
        lambda self, cap: (
            types.SimpleNamespace(run=motion_run) if cap == ctl.CAP_MOTION else real_action(self, cap)
        ),
    )
    live = ctl.LiveController()
    dock = dockmod.PlateDock(live)
    try:
        select(dock, "CAM_front")
        assert dock.btn_motion.isEnabled()  # the real send_motion module imports
        dock.cb_project.setCurrentIndex(dock.cb_project.findData("tula-wot"))
        live.note_packet("CAM_front", "pkt_hero")
        dock.btn_motion.click()
        assert seen["camera"] == "CAM_front" and seen["hero"] == "pkt_hero"
        assert seen["options"].project_id == "tula-wot"
        assert seen["options"].custody == {"nda": True, "allowed_grades": ["C0", "C1"]}
        assert seen["base"] == stub_daemon["url"]
        assert "Send Motion: CAM_front: Sent, ingesting (packet pkt_move)" in dock.log.toPlainText()
    finally:
        dock.close()
        dock.deleteLater()


# ---- what a send says
def test_send_answers_say_sent_ingesting_already_sent_or_ingest_failed(make_dock):
    dock, fake = make_dock()
    fake.results = [
        {
            "camera": "CAM_main",
            "ok": True,
            "packet_id": "p1",
            "errors": [],
            "status_code": 202,
            "ingest": "ingesting",
        },
        {"camera": "CAM_lobby", "ok": True, "packet_id": "p2", "errors": [], "status_code": 200},
        {
            "camera": "CAM_terrace",
            "ok": False,
            "packet_id": "p3",
            "errors": ["ingest: failed: Z missing"],
            "status_code": 202,
            "ingest": "failed: Z missing",
            "warnings": ["the VFB had a LUT"],
        },
    ]
    select(dock, "CAM_main")
    dock._on_send()
    text = dock.log.toPlainText()
    assert "Send Packet: CAM_main: Sent, ingesting (packet p1)" in text
    assert "Send Packet: CAM_lobby: Already sent (packet p2)" in text
    assert "Send Packet: CAM_terrace: Ingest failed: Z missing" in text
    assert "Send Packet: CAM_terrace: the VFB had a LUT" in text


def test_an_unset_custody_shows_in_amber_after_a_send(make_dock, dockmod):
    fake = ctl.FakeController()
    fake.bound_project = "new-job"  # not on the daemon yet: the first packet creates it, custody unset
    dock, _ = make_dock(fake)
    assert dock.lbl_custody.text() == "custody unset"
    select(dock, "CAM_main")
    dock._on_send()
    assert dock.lbl_custody.text() == "custody unset (C0 only)"
    assert dockmod.WARN in dock.lbl_custody.styleSheet()
    assert "only C0 (local) engines run" in dock.log.toPlainText()
    fake.bound_project = "clubhouse"
    dock.sync_project()
    dock._on_send()
    assert dock.lbl_custody.text() == "C0, C1, C2" and dockmod.WARN not in dock.lbl_custody.styleSheet()


def test_supersedes_last_reaches_the_controller(make_dock):
    dock, fake = make_dock()
    dock.chk_supersedes.setChecked(True)
    dock.chk_supersedes.setChecked(False)
    assert calls(fake, "set_supersede_last") == [("set_supersede_last", True), ("set_supersede_last", False)]


# ---- the canvas-job poller's lifecycle
class FakePoller:
    def __init__(self):
        self.starts: list = []
        self.stops = 0
        self.active = False
        self._running = False

    def start(self, timer):
        self.starts.append(timer)
        self._running = True
        return True

    def stop(self):
        self.stops += 1
        self._running = False

    @property
    def running(self):
        return self._running


def test_the_poller_starts_with_the_dock_and_stops_when_it_closes(make_dock, qt):
    fake = ctl.FakeController()
    fake.poller = FakePoller()
    dock, _ = make_dock(fake)
    assert set(fake.poller_hooks) == {"log", "busy", "on_start", "on_finish"}
    assert len(fake.poller.starts) == 1 and isinstance(fake.poller.starts[0], qt.QtCore.QTimer)
    assert fake.poller.starts[0].parent() is dock and dock.lbl_jobs.text() == "canvas jobs: on"
    dock.close()
    assert fake.poller.stops == 1 and not fake.poller.running
    assert dock.lbl_jobs.text() == "canvas jobs: off (dock closed)"


def test_the_poller_follows_the_dock_widget_around_the_page(qt, dockmod):
    fake = ctl.FakeController()
    fake.poller = FakePoller()
    holder = qt.QtWidgets.QDockWidget("PLATE")
    page = dockmod.PlateDock(fake, holder)
    holder.setWidget(page)
    try:
        holder.show()
        qt.app.processEvents()
        assert fake.poller.running
        holder.close()  # the artist closes the dock: no more canvas jobs
        qt.app.processEvents()
        assert not fake.poller.running and fake.poller.stops >= 1
        holder.show()
        qt.app.processEvents()
        assert fake.poller.running
    finally:
        holder.close()
        holder.deleteLater()


def test_a_canvas_job_holds_the_dock_busy_and_a_click_meanwhile_is_refused(make_dock):
    fake = ctl.FakeController()
    fake.poller = FakePoller()
    dock, _ = make_dock(fake)
    hooks = fake.poller_hooks
    assert hooks["busy"]() is False
    job = {"id": "job_1", "action": "light.match", "camera": "CAM_main", "packet_id": "pkt_1"}
    hooks["on_start"](job)
    assert dock._busy and not dock.btn_send.isEnabled() and hooks["busy"]() is True
    select(dock, "CAM_main")
    fake.calls.clear()
    dock._on_send()  # the dock is busy with the job: ignored
    assert calls(fake, "send_packet") == []
    hooks["on_finish"](job, {"ok": True, "packet_id": "pkt_2"})
    assert not dock._busy and dock.btn_send.isEnabled()
    assert "Canvas job light.match CAM_main" in dock.log.toPlainText()
    assert "Canvas job light.match: done (packet pkt_2)" in dock.log.toPlainText()
    fake.poller.active = True  # a tick is talking to the daemon right now
    dock._on_send()
    assert (
        calls(fake, "send_packet") == [] and "a canvas job is talking to the daemon" in dock.log.toPlainText()
    )
    fake.poller.active = False
    hooks["on_finish"](job, {"ok": False, "detail": "MAXGAFFER_MISSING: no MaxGaffer"})
    assert "Canvas job light.match FAILED: MAXGAFFER_MISSING: no MaxGaffer" in dock.log.toPlainText()


def test_the_poller_waits_while_a_daemon_call_spins_events(make_dock):
    fake = ctl.FakeController()
    fake.poller = FakePoller()
    dock, _ = make_dock(fake)
    seen = []
    dock._run_blocking_io(lambda: seen.append(fake.poller_hooks["busy"]()))
    assert seen == [True] and fake.poller_hooks["busy"]() is False


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


def test_max_main_window_is_none_off_max(dockmod, monkeypatch):
    monkeypatch.setitem(sys.modules, "qtmax", None)
    assert dockmod.max_main_window() is None
    marker = object()
    monkeypatch.setitem(sys.modules, "qtmax", types.SimpleNamespace(GetQMaxMainWindow=lambda: marker))
    assert dockmod.max_main_window() is marker


def test_show_dock_opens_plate_as_a_tab_of_the_shared_dock(dockmod, qt):
    from plate_max import shared

    fake = ctl.FakeController()
    page = dockmod.show_dock(fake)
    try:
        host = shared.get_host()
        assert isinstance(page, dockmod.PlateDock) and host.widget("plate") is page
        assert host.dock.objectName() == shared.DOCK_OBJECT_NAME and host.tab_titles() == ["PLATE"]
        assert shared.get_mux().subscribers("filePostOpen") == ["plate"]  # PLATE_ON: refresh the cameras
        assert dockmod.show_dock(fake) is page and host.builds == {"plate": 1}
        assert dockmod.show_dock(fake, standalone=True) is page  # never a twin PLATE window
    finally:
        dockmod.close_dock()
    assert shared.get_host().dock is None and shared.get_host().panels() == []


def test_bootstrap_launch_goes_through_the_shared_dock(dockmod, qt):
    from plate_max import shared

    bootstrap.reset()
    fake = ctl.FakeController()
    try:
        page = bootstrap.launch(fake)
        assert page is shared.get_host().widget("plate") and isinstance(page, dockmod.PlateDock)
    finally:
        bootstrap.reset()
        dockmod.close_dock()


def test_the_standalone_dock_stays_behind_the_env_switch(dockmod, qt, monkeypatch):
    from plate_max import shared

    monkeypatch.setenv(dockmod.STANDALONE_ENV, "1")
    page = dockmod.show_dock(ctl.FakeController())
    try:
        assert isinstance(page, dockmod.PlateDock) and page.isVisible()
        assert shared.get_host().widget("plate") is None
    finally:
        dockmod.close_dock()


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
