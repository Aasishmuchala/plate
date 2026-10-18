"""Wave 12 stress fixes for Send (decision 81): what the render would waste is caught before it.

The max-plugin attacker's repros, on the stub daemon and simulated V-Ray of test_send_packet
(imported helpers only, so no test is collected twice):

  dup-node-names-editable           two geometry nodes named 'Shared' (ARCH_walls + VEG_tree_01): in
                                    either order the Send stops at lint with DUPLICATE_NODE_NAME, no
                                    render, never "last wins"
  duplicate-camera-name             two cameras named CAM_dup: DUPLICATE_CAMERA_NAME, no render
  nonlatin-camera-name              '客户A', Arabic, emoji-only and blank names: BAD_SHOT_SLUG at lint,
                                    no render, no packet folder
  degenerate-lens-values            focal -1 / NaN, f-number 0, film -36, zoom 0: BAD_LENS naming the
                                    property, no render
  nan-inf-pose-accepted             a NaN position and an infinite axis: BAD_POSE, no render; manifests
                                    are strict JSON (a NaN is a manifest-stage SendAbort)
  orphan folder                     a manifest-stage abort removes the folder it leaves without a
                                    manifest (keep_failed_packets keeps it)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fake_pymxs import SCENE_CAMERA_ROWS, FakeCamera, FakeMatrix3, make_node

from plate_max import fsutil, packet, policy
from plate_max.actions import send_packet

pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, send, serve_daemon, vray_scene  # noqa: E402

NAN, INF = float("nan"), float("inf")


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


def add_camera(scene, name: str, rows=SCENE_CAMERA_ROWS, **props) -> FakeCamera:
    """A clean V-Ray physical camera (the attacker's lens values unless props replace them)."""
    lens = {"focal_length": 24.0, "f_number": 8.0, "shutter_speed": 125, **props}
    return scene.add_camera(FakeCamera(name, rows, **lens))


def rows_with(i: int, j: int, value: float):
    rows = [list(r) for r in SCENE_CAMERA_ROWS]
    rows[i][j] = value
    return tuple(tuple(r) for r in rows)


def assert_nothing_rendered(scene, vray, packets: Path, daemon) -> None:
    assert scene.render_calls == [] and vray.written == []
    assert list(packets.iterdir()) == []  # no packet folder, not even an empty one
    assert daemon.hasher.requests == [] and daemon.posted == []


def codes(result) -> list[str]:
    return [p["code"] for p in result.problems]


# --------------------------------------------------------------------------- duplicate node names


def shared_scene(install_scene, order: str):
    """The attacker's scene: 'Shared' twice (on ARCH_walls and on VEG_tree_01, in order) plus
    ARCH_other, covering 50% and 45% of the frame."""
    scene, vray = vray_scene(install_scene)
    nodes = {
        "A": make_node("Shared", "ARCH_walls", "MAT_brick", x=0.0),
        "V": make_node("Shared", "VEG_tree_01", "MAT_leaf", x=100.0),
    }
    scene.objects = [c for c in scene.objects if c.super_class != "GeometryClass"]
    scene.objects += [nodes[k] for k in order] + [make_node("ARCH_other", "ARCH_walls", "MAT_brick")]
    vray.shares = {"Shared": 0.5, "ARCH_other": 0.45}
    return scene, vray


@pytest.mark.parametrize("order", ["AV", "VA"])
def test_two_nodes_named_shared_stop_the_send_at_lint_in_either_order(install_scene, daemon_env, order):
    daemon, cfg, packets = daemon_env
    scene, vray = shared_scene(install_scene, order)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "lint"
    assert codes(result) == [policy.DUPLICATE_NODE_NAME]
    problem = result.problems[0]
    assert problem["node"] == "Shared"
    assert problem["detail"].startswith("duplicate visible node name 'Shared'")  # legend's own words
    assert "'ARCH_walls'" in problem["detail"] and "'VEG_tree_01'" in problem["detail"]
    assert result.errors[0].startswith("lint: duplicate visible node name 'Shared'")
    assert result.packet_id is None and result.render is None
    assert_nothing_rendered(scene, vray, packets, daemon)


@pytest.mark.parametrize("order", ["AV", "VA"])
def test_legend_nodes_never_lets_the_last_node_decide(install_scene, order):
    """The post-render net: even past the lint, the id of 'Shared' is never classed by one layer."""
    from plate_max import digest, legend
    from plate_max.exr_header import Coverage

    shared_scene(install_scene, order)
    hex_shared = legend.cryptomatte_hash("Shared")
    coverage = Coverage(layer="CryptoObject", shares={hex_shared: 0.5}, pixels=100, compression="zip")
    with pytest.raises(send_packet.SendAbort) as caught:
        send_packet.legend_nodes({"Shared": hex_shared}, coverage, digest.scene_nodes())
    assert caught.value.stage == "lint"
    assert [p["code"] for p in caught.value.problems] == [policy.DUPLICATE_NODE_NAME]
    assert "duplicate visible node name 'Shared'" in caught.value.reason


def test_a_hidden_duplicate_is_refused_too(install_scene):
    """V-Ray can render hidden geometry, so a hidden twin on an editable layer may not decide either."""
    scene, _ = vray_scene(install_scene)
    scene.objects.append(make_node("ARCH_wall_north", "VEG_tree_01", "MAT_leaf", isHidden=True))
    report = policy.lint_scene("CAM_front")
    assert report.codes == [policy.DUPLICATE_NODE_NAME]
    assert report.problems[0]["node"] == "ARCH_wall_north"


# --------------------------------------------------------------------------- duplicate camera names


def test_two_cameras_named_cam_dup_stop_at_lint(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    first = add_camera(scene, "CAM_dup")
    second = FakeCamera("CAM_dup", SCENE_CAMERA_ROWS, focal_length=50.0)
    scene.cameras["CAM_dup (second)"] = second  # rt.cameras lists both; the name finds the first
    assert scene.getNodeByName("CAM_dup") is first
    results = send(["CAM_dup", "CAM_dup"], packets, cfg)
    for result in results:
        assert not result.ok and result.stage == "lint"
        assert codes(result) == [policy.DUPLICATE_CAMERA_NAME]
        assert "2 cameras are named 'CAM_dup'" in result.problems[0]["detail"]
    assert_nothing_rendered(scene, vray, packets, daemon)


def test_a_camera_target_does_not_count_as_a_camera(install_scene):
    scene, _ = vray_scene(install_scene)
    target = FakeCamera("CAM_front", SCENE_CAMERA_ROWS)
    target.base_class = "Targetobject"
    scene.cameras["CAM_front target"] = target
    assert policy.lint_scene("CAM_front").passed


# --------------------------------------------------------------------------- shot slugs


@pytest.mark.parametrize(
    "name",
    ["客户A", "كاميرا_رئيسية", "🎥🏠", "   ", "कैमरा_१", "カメラ主", ".."],
    ids=["cjk-with-A", "arabic", "emoji-only", "spaces", "devanagari", "cjk", "dots"],
)
def test_a_camera_name_that_cannot_be_a_shot_id_stops_at_lint(install_scene, daemon_env, name):
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    add_camera(scene, name)
    [result] = send([name], packets, cfg)
    assert not result.ok and result.stage == "lint"
    assert codes(result) == [policy.BAD_SHOT_SLUG]
    assert result.problems[0]["node"] == name and repr(name) in result.problems[0]["detail"]
    assert "rename" in result.problems[0]["detail"]
    assert_nothing_rendered(scene, vray, packets, daemon)


@pytest.mark.parametrize(
    ("name", "slug"),
    [("CAM_🏠", "cam"), ("Cam 1", "cam-1"), ("CAM_FRONT", "cam-front"), ("C" * 80, "c" * 64)],
)
def test_ascii_names_with_symbols_still_have_a_shot_id(name, slug):
    assert policy.shot_slug_problems(name) == []
    assert packet.slugify(name) == slug


def test_the_shot_and_project_ids_exist_before_the_render(install_scene, daemon_env, monkeypatch):
    """A project id that cannot be a slug is refused at lint, before V-Ray is asked for anything."""
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    [result] = send(["CAM_front"], packets, cfg, project_id="客户")
    assert not result.ok and result.stage == "lint" and result.errors[0].startswith("project: ")
    assert_nothing_rendered(scene, vray, packets, daemon)
    seen: list[tuple[str, str]] = []
    real = send_packet._render_and_write

    def spy(*args, **kw):
        seen.append((kw["shot_id"], kw["project_id"]))
        return real(*args, **kw)

    monkeypatch.setattr(send_packet, "_render_and_write", spy)
    [ok] = send(["CAM_front"], packets, cfg)
    assert ok.ok, (ok.stage, ok.errors)
    assert seen == [("cam-front", "tula-wot")]


# --------------------------------------------------------------------------- lens and exposure values


LENS_CASES = {
    "focal-minus-1": ({"focal_length": -1.0}, "focal_length"),
    "f-number-0": ({"f_number": 0.0}, "f_number"),
    "focal-nan": ({"focal_length": NAN}, "focal_length"),
    "film-minus-36": ({"film_width": -36.0}, "film_width"),
    "film-0": ({"film_width": 0.0}, "film_width"),
    "zoom-0": ({}, "zoom_factor"),  # set after construction: the fake derives fov from the zoom
    "iso-0": ({"ISO": 0}, "iso"),
    "shutter-0": ({"shutter_speed": 0}, "shutter_speed"),
    "shutter-nan": ({"shutter_speed": NAN}, "shutter_speed"),
}


@pytest.mark.parametrize("case", list(LENS_CASES))
def test_degenerate_lens_values_are_bad_lens_before_the_render(install_scene, daemon_env, case):
    daemon, cfg, packets = daemon_env
    props, prop = LENS_CASES[case]
    scene, vray = vray_scene(install_scene)
    cam = add_camera(scene, "CAM_lens", **props)
    if case == "zoom-0":
        cam.zoom_factor = 0.0
    [result] = send(["CAM_lens"], packets, cfg)
    assert not result.ok and result.stage == "lint", (result.stage, result.errors)
    assert codes(result) == [policy.BAD_LENS]
    detail = result.problems[0]["detail"]
    assert prop in detail and "CAM_lens" in detail and "ZeroDivisionError" not in detail
    assert_nothing_rendered(scene, vray, packets, daemon)


def test_the_shutter_detail_shows_the_value_the_artist_typed(install_scene):
    scene, _ = vray_scene(install_scene)
    add_camera(scene, "CAM_shutter", shutter_speed=0)
    [problem] = policy.lint_scene("CAM_shutter").problems
    assert "shutter_speed is 0, not positive" in problem["detail"]
    scene.cameras["CAM_shutter"].shutter_speed = -50
    assert "shutter_speed is -50, not positive" in policy.lint_scene("CAM_shutter")[0]


def test_a_non_positive_film_gate_is_never_assumed_36mm(install_scene):
    scene, _ = vray_scene(install_scene)
    add_camera(scene, "CAM_film", film_width=-36.0)
    from plate_max import digest

    cam = digest.camera_digest("CAM_film")
    assert cam["film_gate_mm"] == -36.0 and cam["intrinsics_px"] is None
    assert not any("assumed" in w for w in cam["warnings"])
    with pytest.raises(packet.ManifestError, match="film gate"):
        packet.build_camera_block(cam)


def test_every_degenerate_value_is_reported_not_just_the_first(install_scene):
    scene, _ = vray_scene(install_scene)
    add_camera(scene, "CAM_bad", focal_length=-1.0, f_number=0.0, film_width=NAN)
    report = policy.lint_scene("CAM_bad")
    assert report.codes == [policy.BAD_LENS] * 3
    assert [p["detail"].split(" ")[2] for p in report.problems] == ["focal", "film", "f-number"]


# --------------------------------------------------------------------------- the camera pose


@pytest.mark.parametrize(
    ("rows", "where"),
    [
        (rows_with(3, 0, NAN), "position (row4) x"),
        (rows_with(3, 1, INF), "position (row4) y"),
        (rows_with(0, 0, INF), "X axis (row1) x"),
        (rows_with(0, 0, NAN), "X axis (row1) x"),
    ],
    ids=["nan-position", "inf-position", "inf-axis", "nan-axis"],
)
def test_a_non_finite_pose_is_bad_pose_before_the_render(install_scene, daemon_env, rows, where):
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    add_camera(scene, "CAM_pose", rows=rows)
    [result] = send(["CAM_pose"], packets, cfg)
    assert not result.ok and result.stage == "lint"
    assert codes(result) == [policy.BAD_POSE]  # never also SCALED_CAMERA
    assert where in result.problems[0]["detail"]
    assert_nothing_rendered(scene, vray, packets, daemon)


def test_a_scaled_camera_is_still_scaled_camera(install_scene):
    scene, _ = vray_scene(install_scene)
    add_camera(scene, "CAM_scaled2", rows=((2, 0, 0), (0, 2, 0), (0, 0, 2), (0, 0, 0)))
    assert policy.lint_scene("CAM_scaled2").codes == [policy.SCALED_CAMERA]


def test_a_pose_that_turns_nan_after_lint_is_refused_by_the_writer(install_scene):
    scene, _ = vray_scene(install_scene)
    from plate_max import digest

    cam = digest.camera_digest("CAM_front")
    scene.cameras["CAM_front"].objecttransform = FakeMatrix3(rows_with(3, 2, NAN))
    bad = digest.camera_digest("CAM_front")
    assert any(digest.NON_FINITE_POSE in w for w in bad["warnings"])
    with pytest.raises(packet.ManifestError, match="non-finite"):
        packet.build_camera_block(bad)
    assert packet.build_camera_block(cam)["name"] == "CAM_front"


# --------------------------------------------------------------------------- strict JSON and orphans


def test_dump_json_bytes_refuses_nan_and_infinity():
    for value in (NAN, INF, -INF):
        with pytest.raises(ValueError):
            fsutil.dump_json_bytes({"a": value})
        with pytest.raises(ValueError):
            packet.legend_bytes({"packet_id": "x", "entries": [{"pixel_share": value}]})
    assert json.loads(fsutil.dump_json_bytes({"a": 1.5})) == {"a": 1.5}


def test_a_forced_manifest_abort_leaves_no_orphan_folder(install_scene, daemon_env, monkeypatch):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)

    def refuse(*args, **kw):
        raise packet.ManifestError("forced for the test")

    monkeypatch.setattr(send_packet.packet, "build_manifest", refuse)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "manifest" and "forced for the test" in result.errors[0]
    assert len(scene.render_calls) == 1  # it did render: the folder existed until the abort
    assert list(packets.iterdir()) == [] and result.packet_dir is None and result.packet_id
    assert any("was removed" in w and result.packet_id in w for w in result.warnings)
    assert not any("kept" in w for w in result.warnings)
    assert daemon.posted == []


def test_keep_failed_packets_keeps_the_folder(install_scene, daemon_env, monkeypatch):
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    monkeypatch.setattr(
        send_packet.packet,
        "build_manifest",
        lambda *a, **k: (_ for _ in ()).throw(packet.ManifestError("forced")),
    )
    [result] = send(["CAM_front"], packets, cfg, keep_failed_packets=True)
    assert result.stage == "manifest" and result.packet_dir
    root = Path(result.packet_dir)
    assert (root / "beauty.exr").is_file() and not (root / "manifest.json").exists()
    assert any("kept" in w and "no manifest" in w for w in result.warnings)


def test_a_nan_in_the_manifest_is_a_manifest_abort_not_a_file(install_scene, daemon_env, monkeypatch):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    real = packet.build_manifest

    def with_nan(*args, **kw):
        manifest = real(*args, **kw)
        manifest["render"]["time_s"] = NAN
        return manifest

    monkeypatch.setattr(send_packet.packet, "build_manifest", with_nan)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "manifest"
    assert result.errors[0].startswith("manifest: a value has no JSON form")
    assert list(packets.iterdir()) == [] and daemon.posted == []


def test_an_earlier_failure_still_keeps_the_render(install_scene, daemon_env):
    """A hash-stage failure is not an orphan by this rule: the render stays for inspection."""
    daemon, cfg, _ = daemon_env
    vray_scene(install_scene)
    elsewhere = Path(daemon.packets_dirs[0]).parent / "elsewhere"
    daemon.packets_dirs.append(str(elsewhere))
    [result] = send(["CAM_front"], elsewhere, cfg)
    assert result.stage == "hash" and Path(result.packet_dir).is_dir()
    assert any("kept" in w for w in result.warnings)


def test_remove_tree_reports_a_folder_already_gone(tmp_path):
    assert fsutil.remove_tree(tmp_path / "never-made")
    folder = tmp_path / "pkt"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "x.bin").write_bytes(b"x")
    assert fsutil.remove_tree(folder) and not folder.exists()


def test_the_clean_clubhouse_camera_still_sends(install_scene, daemon_env):
    """The control: a clean scene passes every new check and sends as before."""
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    [result] = send(["CAM_front"], packets, cfg)
    assert result.ok, (result.stage, result.errors)
    text = (Path(result.packet_dir) / "manifest.json").read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    manifest = json.loads(text)
    assert all(math.isfinite(v) for row in manifest["camera"]["world_from_camera"] for v in row)
