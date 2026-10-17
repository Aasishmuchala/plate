"""recipes (CameraRecipe and the <scene>.plate.json sidecar), actions.batch_cameras and
actions.receive on the fake scene. Batch runs reuse test_send_packet's simulated V-Ray and stub
daemon (imported helpers only, so no test is collected twice)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import plate_spec
from plate_max import rt as rt_door
from plate_max.actions import batch_cameras, receive
from plate_max.recipes import (
    CameraRecipe,
    RecipeError,
    default_editable_classes,
    get_recipe,
    load_recipes,
    save_recipes,
    set_recipe,
    sidecar_path,
)

pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, FakeVRay, gamma_colour, serve_daemon  # noqa: E402


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


@pytest.fixture
def daemon_env(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def small_clubhouse(install_scene, size=(160, 90)):
    scene = install_scene("clubhouse")
    scene.renderWidth, scene.renderHeight = size
    gamma_colour(scene)
    return scene, FakeVRay(scene)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- recipes


def test_recipe_defaults_follow_the_policy():
    rec = CameraRecipe(camera="CAM_a")
    assert rec.editable_classes == default_editable_classes()
    assert set(rec.editable_classes) == {"vegetation", "sky", "people", "water", "furniture"}
    assert rec.safe_frame is None and rec.overscan_aspect is None and rec.extra_elements == []
    assert rec.engine_id == "nano-banana-2.1"


def test_recipe_normalises_and_refuses():
    rec = CameraRecipe(
        camera="C", safe_frame={"x": 0, "y": 138, "width": 1920, "height": 804}, overscan_aspect=" 16:9 "
    )
    assert rec.safe_frame == {"x": 0, "y": 138, "w": 1920, "h": 804} and rec.overscan_aspect == "16:9"
    assert CameraRecipe(camera="C", safe_frame=(1, 2, 3, 4)).safe_frame == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert CameraRecipe(camera="C", overscan_aspect="").overscan_aspect is None
    for bad in (
        {"editable_classes": ["vegetation", "architecture"]},
        {"editable_classes": ["glazing"]},
        {"editable_classes": ["other"]},
        {"overscan_aspect": "wide"},
        {"overscan_aspect": "16:0"},
        {"safe_frame": {"x": 0, "y": 0, "w": 0, "h": 10}},
        {"safe_frame": "full"},
        {"extra_elements": ["VRayMagicPass"]},
    ):
        with pytest.raises(RecipeError):
            CameraRecipe(camera="C", **bad)
    with pytest.raises(RecipeError):
        CameraRecipe(camera="")


def test_recipe_coerce_takes_dicts_objects_and_names():
    as_dict = CameraRecipe.coerce({"camera": "A", "preset": "night-relight", "unknown_key": 1})
    assert as_dict.preset == "night-relight"
    obj = SimpleNamespace(camera="B", engine_id="gpt-image-2.5", overscan_aspect="3:2")
    assert CameraRecipe.coerce(obj).overscan_aspect == "3:2"
    assert CameraRecipe.coerce(None, "C").camera == "C"
    renamed = CameraRecipe.coerce(CameraRecipe(camera="A"), "Z")
    assert renamed.camera == "Z"
    assert CameraRecipe.from_dict(as_dict.to_dict()) == as_dict


def test_sidecar_sits_next_to_the_max_file(tmp_path):
    scene = tmp_path / "Clubhouse v3.max"
    assert sidecar_path(scene) == tmp_path / "Clubhouse v3.plate.json"
    assert sidecar_path(str(tmp_path / "noext")) == tmp_path / "noext.plate.json"
    assert sidecar_path("") is None and sidecar_path(None) is None


def test_recipes_round_trip_through_the_sidecar(tmp_path):
    scene = tmp_path / "clubhouse.max"
    recipes = {
        "CAM_front": CameraRecipe(camera="CAM_front", overscan_aspect="16:9", preset="sunburst-rephotograph"),
        "CAM_entry": {
            "camera": "CAM_entry",
            "editable_classes": ["vegetation"],
            "safe_frame": [0, 10, 100, 50],
        },
    }
    path = save_recipes(recipes, scene)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    doc = json.loads(raw.decode("utf-8"))
    assert doc["format_version"] == 1 and sorted(doc["cameras"]) == ["CAM_entry", "CAM_front"]
    loaded = load_recipes(scene)
    assert loaded["CAM_front"].overscan_aspect == "16:9"
    assert loaded["CAM_entry"].safe_frame == {"x": 0, "y": 10, "w": 100, "h": 50}
    # set_recipe keeps the others; get_recipe falls back to the default
    set_recipe(CameraRecipe(camera="CAM_aerial", engine_id="flux-3-image"), scene)
    assert sorted(load_recipes(scene)) == ["CAM_aerial", "CAM_entry", "CAM_front"]
    assert get_recipe("CAM_new", scene) == CameraRecipe(camera="CAM_new")
    assert not list(tmp_path.glob("*.tmp"))


def test_a_malformed_sidecar_entry_is_skipped(tmp_path):
    scene = tmp_path / "s.max"
    sidecar_path(scene).write_text(
        json.dumps({"cameras": {"ok": {"preset": "x"}, "bad": {"editable_classes": ["signage"]}, "junk": 3}}),
        encoding="utf-8",
        newline="\n",
    )
    assert sorted(load_recipes(scene)) == ["ok"]
    sidecar_path(scene).write_text("{broken", encoding="utf-8")
    assert load_recipes(scene) == {}


def test_an_unsaved_scene_has_no_sidecar(scene):
    assert load_recipes() == {}
    with pytest.raises(RecipeError, match="saved"):
        save_recipes({"CAM_main": CameraRecipe(camera="CAM_main")})


def test_the_open_scene_sidecar_is_used_by_default(scene, tmp_path):
    scene.maxFilePath, scene.maxFileName = str(tmp_path), "open.max"
    set_recipe({"camera": "CAM_main", "preset": "crop-pair-pushin"})
    assert (tmp_path / "open.plate.json").is_file()
    assert get_recipe("CAM_main").preset == "crop-pair-pushin"


def test_the_dock_builds_recipes_from_its_dicts():
    """The dock's LiveController hands CameraRecipe(**default_recipe(camera)) to the actions."""
    from plate_max.ui.controller import default_recipe

    rec = CameraRecipe(**default_recipe("CAM_x"))
    assert rec.camera == "CAM_x" and rec.editable_classes == default_editable_classes()


# --------------------------------------------------------------------------- batch_cameras


def test_batch_sends_every_recipe_and_writes_a_summary(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = small_clubhouse(install_scene)
    progress: list[tuple[int, int, str]] = []
    recipes = [
        CameraRecipe(camera="CAM_front", overscan_aspect="16:9"),
        {"camera": "CAM_aerial", "editable_classes": ["sky"]},
        "CAM_entry",
    ]
    results = batch_cameras.run(
        recipes,
        packets,
        cfg["daemon_url"],
        {"config": cfg, "project_id": "tula-wot", "progress": lambda *a: progress.append(a)},
    )
    assert [r.camera for r in results] == ["CAM_front", "CAM_aerial", "CAM_entry"]
    assert all(r.ok for r in results), [r.errors for r in results]
    for r in results:
        assert plate_spec.validate_packet_dir(r.packet_dir, check_files=True) == []
        assert read_json(Path(r.packet_dir) / "manifest.json")["render"]["session"] == "batch"
    front = read_json(Path(results[0].packet_dir) / "manifest.json")
    assert front["camera"]["overscan"]["engine_aspect"] == "16:9"  # 160x90 is 16:9 already

    [summary] = list(packets.glob("batch_*.json"))
    assert ":" not in summary.name and results[0].batch_summary == str(summary)
    doc = read_json(summary)
    assert doc["cameras"] == 3 and doc["sent"] == 3 and doc["failed"] == 0
    assert [r["camera"] for r in doc["results"]] == ["CAM_front", "CAM_aerial", "CAM_entry"]
    assert [r["packet_id"] for r in doc["results"]] == [r.packet_id for r in results]
    dones = [p[0] for p in progress]
    assert dones == sorted(dones) and progress[-1][:2] == (3, 3) and "batch done" in progress[-1][2]
    assert len(scene.render_calls) == 3


def test_a_bad_recipe_fails_alone(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    small_clubhouse(install_scene)
    results = batch_cameras.run(
        [{"camera": "CAM_front", "editable_classes": ["architecture"]}, CameraRecipe(camera="CAM_entry")],
        packets,
        cfg["daemon_url"],
        {"config": cfg},
    )
    assert [r.ok for r in results] == [False, True]
    assert results[0].stage == "recipe" and "never" in results[0].errors[0]
    doc = read_json(next(packets.glob("batch_*.json")))
    assert doc["sent"] == 1 and doc["failed"] == 1


def test_two_batches_in_one_second_keep_both_summaries(tmp_path):
    first = batch_cameras.summary_path(tmp_path, "20261017-120000")
    first.write_text("{}", encoding="utf-8")
    second = batch_cameras.summary_path(tmp_path, "20261017-120000")
    assert second.name == "batch_20261017-120000-2.json"


def test_an_empty_batch_writes_an_empty_summary(tmp_path):
    assert batch_cameras.run([], tmp_path, "http://127.0.0.1:9", {}) == []
    doc = read_json(next(tmp_path.glob("batch_*.json")))
    assert doc["cameras"] == 0 and doc["results"] == []


# --------------------------------------------------------------------------- receive


@pytest.fixture
def returned_image(tmp_path):
    path = tmp_path / "returned" / "pkt_X_nano.png"
    path.parent.mkdir()
    from fake_pymxs import tiny_png

    path.write_bytes(tiny_png())
    return path


def test_receive_into_the_vfb_history(scene, returned_image):
    ok, message = receive.load_result(returned_image, "vfb_history")
    assert ok and "VFB history" in message
    calls = [(str(c[0]), *c[1:]) for c in scene.vfb_calls]
    assert calls[0] == ("loadimage", str(returned_image.resolve()))
    assert calls[1] == ("historysave",)
    assert scene.render_calls == [] and scene.save_calls == []  # display only


def test_receive_without_a_history_command_still_loads(scene, returned_image):
    def vfb(*args):
        if str(args[0]).lower() == "historysave":
            raise RuntimeError("unknown command")
        scene.vfb_calls.append(args)

    scene.vfbControl = vfb
    ok, message = receive.load_result(returned_image)
    assert ok and "compare it now" in message


def test_receive_into_the_viewport_background(scene, returned_image):
    redraws = []
    scene.viewport = SimpleNamespace(DispBkgImage=False)
    scene.redrawViews = lambda: redraws.append(1)
    ok, message = receive.load_result(returned_image, "viewport_background")
    assert ok and "viewport background" in message
    assert scene.backgroundImageFileName == str(returned_image.resolve())
    assert scene.viewport.DispBkgImage is True and redraws == [1]


def test_receive_without_a_viewport_switch_says_so(scene, returned_image):
    ok, message = receive.load_result(returned_image, "viewport_background")
    assert ok and "Show Background" in message


@pytest.mark.parametrize(
    ("path_kind", "mode", "needle"),
    [
        ("missing", "vfb_history", "no such file"),
        ("image", "screen", "unknown receive mode"),
        ("text", "vfb_history", "not an image"),
    ],
)
def test_receive_refuses_bad_requests(scene, returned_image, tmp_path, path_kind, mode, needle):
    path = {"missing": tmp_path / "nope.png", "image": returned_image, "text": tmp_path / "notes.txt"}[
        path_kind
    ]
    if path_kind == "text":
        path.write_text("hi", encoding="utf-8")
    ok, message = receive.load_result(path, mode)
    assert not ok and needle in message and scene.vfb_calls == []


def test_receive_without_vray_or_max(scene, returned_image):
    scene.vfbControl = None
    ok, message = receive.load_result(returned_image, "vfb_history")
    assert not ok and "vfbControl" in message
    rt_door.set_runtime(None)
    ok, message = receive.load_result(returned_image, "vfb_history")
    assert not ok and "pymxs" in message
