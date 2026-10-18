"""bridge.camera_level and Send's level-and-crop path (decision 58) on the fake scene.

The identity vector settles the sign of dy: a tilt-corrected (two-point) camera pitched by 2, 3.55 and
8 degrees (and down by 3.55) is modelled the way the optics say it renders, a pinhole with the level
pose whose principal point puts the pitched aim point at the frame centre, built here from the yaw and
the pitch alone and not from camera_level's formula. Five world points projected through that model
must land where the level plan's camera puts them, minus the safe frame's origin, within 0.5 px.

The Send half reuses test_send_packet's simulated V-Ray and stub daemon (imported helpers only, so no
test is collected twice). LinkedCamera is a FakeCamera whose objecttransform follows transform through
the object offset, as in Max, where objecttransform is read-only; the fake is also taught copy, delete,
matrix3 and Point3, the calls the temporary level camera needs.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest
from fake_pymxs import FakeCamera, FakeMatrix3, FakePoint3

import plate_spec
from plate_max import digest, packet, policy
from plate_max.actions import send_packet
from plate_max.bridge import camera_level
from plate_max.recipes import CameraRecipe

np = pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, FakeVRay, gamma_colour, serve_daemon  # noqa: E402

YAW = 30.0
POS = (12000.0, -8500.0, 1600.0)
FOCAL, FILM = 24.0, 36.0
W, H = 3840, 2160
PITCHES = (2.0, 3.55, 8.0, -3.55)
PIXELS = (
    (0.1, 0.1, 4000.0),
    (0.9, 0.12, 9000.0),
    (0.5, 0.5, 15000.0),
    (0.15, 0.88, 6000.0),
    (0.92, 0.9, 25000.0),
)


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


# --------------------------------------------------------------------------- geometry helpers


def forward(yaw: float, pitch: float) -> np.ndarray:
    y, p = math.radians(yaw), math.radians(pitch)
    return np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])


def pose_rows(yaw: float, pitch: float, pos=POS, roll: float = 0.0) -> tuple:
    """Max rows (right, up, back, position) of a camera heading yaw degrees from +X toward +Y, pitched
    up by pitch degrees and rolled by roll degrees about its view axis."""
    f = forward(yaw, pitch)
    right = np.array([math.sin(math.radians(yaw)), -math.cos(math.radians(yaw)), 0.0])
    up = np.cross(right, f)
    c, s = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    right, up = c * right + s * up, -s * right + c * up
    return tuple(tuple(float(v) for v in row) for row in (right, up, -f, np.array(pos)))


def pinhole(rows, fx: float, pp) -> dict[str, Any]:
    return {
        "world_from_camera": plate_spec.world_from_camera_from_max_rows(rows),
        "intrinsics_px": {"fx": fx, "fy": fx, "principal_point": [float(pp[0]), float(pp[1])]},
    }


def tilt_corrected(yaw: float, pitch: float, fx: float, width: int, height: int) -> dict[str, Any]:
    """The two-point camera as the optics render it: the image plane stays vertical (the level pose)
    and the pitched aim point lands at the frame centre."""
    rows = pose_rows(yaw, 0.0)
    centred = pinhole(rows, fx, (width / 2, height / 2))
    aim = np.array(POS) + 10000.0 * forward(yaw, pitch)
    ua, va, _ = plate_spec.project_point(centred, aim)
    return pinhole(rows, fx, (width - ua, height - va))


def unproject(model: dict[str, Any], u: float, v: float, depth: float) -> list[float]:
    m = np.array(model["world_from_camera"], dtype=float)
    k = model["intrinsics_px"]
    cx, cy = k["principal_point"]
    cam = np.array([(u - cx) / k["fx"] * depth, -(v - cy) / k["fy"] * depth, -depth])
    return (m[:3, :3] @ cam + m[:3, 3]).tolist()


def five_points(model: dict[str, Any], width: int, height: int) -> list[list[float]]:
    return [unproject(model, a * width, b * height, d) for a, b, d in PIXELS]


def assert_crop_identity(original, level_cam, safe, width, height, tol=0.5):
    for p in five_points(original, width, height):
        u0, v0, _ = plate_spec.project_point(original, p)
        u1, v1, _ = plate_spec.project_point(level_cam, p)
        assert abs(u0 - (u1 - safe["x"])) <= tol + 1e-9, (p, u0, u1)
        assert abs(v0 - (v1 - safe["y"])) <= tol + 1e-9, (p, v0, v1)


def add_camera(rt, name: str, rows, cls=FakeCamera, **props):
    cam = cls(name, rows, focal_length=FOCAL, film_width=FILM, f_number=8.0, shutter_speed=125, **props)
    rt.add_camera(cam)
    rt.objects.append(cam)
    return cam


# --------------------------------------------------------------------------- the identity vector


@pytest.mark.parametrize("pitch", PITCHES)
def test_the_level_crop_reproduces_the_tilt_corrected_camera(scene, pitch):
    add_camera(scene, "CAM_tilt", pose_rows(YAW, pitch), auto_vertical_tilt=True)
    cam = digest.camera_digest("CAM_tilt")
    level = camera_level.plan(cam, W, H)
    assert level is not None and level.mode == camera_level.TILT and level.source == "auto_vertical_tilt"

    fx = FOCAL * W / FILM
    dy = fx * math.tan(math.radians(pitch))
    pad = math.ceil(abs(dy))
    assert level.pitch_deg == pytest.approx(pitch, abs=1e-9)
    assert level.dy_px == pytest.approx(dy, rel=1e-12) and level.pad_px == pad
    assert (level.width, level.height, level.render_height) == (W, H, H + 2 * pad)
    assert level.safe_frame == {"x": 0, "y": round(pad - dy), "width": W, "height": H}
    assert abs(level.residual_px) <= 0.5
    assert level.film_height_scale == pytest.approx((H + 2 * pad) / H)

    # the level pose: the same position and heading, pitch zero, up is world +Z
    want = plate_spec.world_from_camera_from_max_rows(pose_rows(YAW, 0.0))
    assert np.allclose(level.world_from_camera, want, atol=1e-12)

    original = tilt_corrected(YAW, pitch, fx, W, H)
    # the model is the camera: its aim point is where the node's own (pitched, untilted) pinhole has it
    aim = np.array(POS) + 10000.0 * forward(YAW, pitch)
    assert plate_spec.project_point(pinhole(pose_rows(YAW, pitch), fx, (W / 2, H / 2)), aim)[:2] == (
        pytest.approx(W / 2),
        pytest.approx(H / 2),
    )
    assert plate_spec.project_point(original, aim)[:2] == (pytest.approx(W / 2), pytest.approx(H / 2))
    assert_crop_identity(original, camera_level.level_camera(level), level.safe_frame, W, H)
    # the module's own description of the original agrees with the optics
    mine = camera_level.original_camera(level)["intrinsics_px"]["principal_point"]
    assert mine == pytest.approx(original["intrinsics_px"]["principal_point"], abs=1e-6)


def test_looking_up_crops_the_top_and_looking_down_the_bottom(scene):
    add_camera(scene, "CAM_up", pose_rows(YAW, 8.0), auto_vertical_tilt=True)
    add_camera(scene, "CAM_down", pose_rows(YAW, -8.0), auto_vertical_tilt=True)
    up = camera_level.plan(digest.camera_digest("CAM_up"), W, H)
    down = camera_level.plan(digest.camera_digest("CAM_down"), W, H)
    assert up.dy_px > 0 and up.safe_y <= 1
    assert down.dy_px < 0 and down.safe_y >= 2 * down.pad_px - 1
    assert up.pad_px == down.pad_px and up.render_height == down.render_height


def test_a_manual_vertical_tilt_is_levelled_at_the_cameras_pitch(scene):
    add_camera(scene, "CAM_vt", pose_rows(YAW, 3.55), vertical_tilt=1.0)
    level = camera_level.plan(digest.camera_digest("CAM_vt"), W, H)
    assert level is not None and level.source == "vertical_tilt"
    assert level.pitch_deg == pytest.approx(3.55)
    assert "vertical_tilt 1 read as the full correction" in level.note


def test_a_vertical_shift_keeps_the_pose_and_moves_the_frame_by_the_shift(scene):
    rows = pose_rows(YAW, 0.0)
    add_camera(scene, "CAM_rise", rows, vertical_shift=0.1)
    cam = digest.camera_digest("CAM_rise")
    level = camera_level.plan(cam, W, H)
    assert level is not None and level.mode == camera_level.SHIFT and level.source == "vertical_shift"
    assert level.dy_px == pytest.approx(0.1 * H) and level.render_height == H + 2 * 216
    assert level.safe_frame == {"x": 0, "y": 0, "width": W, "height": H}  # a raised view: the top
    assert np.allclose(level.world_from_camera, cam["world_from_camera"])
    assert "vertically shifted camera: shift 0.1" in level.note and "unverified" in level.note
    # the shifted camera as a pinhole: its own pose, the principal point s * height below the centre
    fx = FOCAL * W / FILM
    original = pinhole(rows, fx, (W / 2, H / 2 + 0.1 * H))
    assert_crop_identity(original, camera_level.level_camera(level), level.safe_frame, W, H)


def test_a_plain_pinhole_has_no_plan(scene):
    add_camera(scene, "CAM_plain", pose_rows(YAW, 5.0))
    assert camera_level.assess(digest.camera_digest("CAM_plain"), W, H) == (None, "")


@pytest.mark.parametrize(
    "props, rows, words",
    [
        ({"auto_vertical_tilt": True, "distortion": 0.03}, pose_rows(YAW, 3.0), "distortion"),
        ({"horizontal_shift": 0.1}, pose_rows(YAW, 0.0), "horizontal"),
        ({"auto_vertical_tilt": True, "horizontal_tilt": 1.0}, pose_rows(YAW, 3.0), "horizontal"),
        ({"auto_vertical_tilt": True, "vertical_shift": 0.1}, pose_rows(YAW, 3.0), "together"),
        ({"auto_vertical_tilt": True}, pose_rows(YAW, 50.0), "limit"),
        ({"auto_vertical_tilt": True}, pose_rows(YAW, 8.0, roll=2.0), "rolled"),
    ],
)
def test_cameras_that_stay_refused_say_why(scene, props, rows, words):
    add_camera(scene, "CAM_x", rows, **props)
    level, why = camera_level.assess(digest.camera_digest("CAM_x"), W, H)
    assert level is None and words in why


def test_a_hairline_roll_is_kept_and_does_not_move_the_frame(scene):
    add_camera(scene, "CAM_r", pose_rows(YAW, 3.55, roll=0.001), auto_vertical_tilt=True)
    level = camera_level.plan(digest.camera_digest("CAM_r"), W, H)
    assert level is not None
    up = np.array(level.world_from_camera)[:3, 1]
    assert up[2] == pytest.approx(math.cos(math.radians(0.001)), abs=1e-12)


# --------------------------------------------------------------------------- lint


def test_lint_warns_instead_of_refusing_when_send_levels_and_crops(scene):
    add_camera(scene, "CAM_tilt", pose_rows(YAW, 3.55), auto_vertical_tilt=True)
    refused = policy.lint_scene("CAM_tilt")
    assert refused.codes == [policy.SHIFTED_CAMERA] and not refused.passed  # the v0.1 default stays
    report = policy.lint_scene("CAM_tilt", level_and_crop=True)
    assert report.passed and report.codes == []
    (warning,) = report.warnings
    assert warning["code"] == policy.SHIFTED_CAMERA and warning["node"] == "CAM_tilt"
    assert "level pinhole at 3840x" in warning["detail"] and "pitch 3.550 deg" in warning["detail"]
    assert report.as_dict()["warnings"] == [warning] and report["warnings"] == [warning]
    assert "warnings" not in policy.lint_scene("CAM_main").as_dict()


def test_lint_keeps_refusing_what_level_and_crop_cannot_send(install_scene):
    install_scene("shifted_camera")
    report = policy.lint_scene("CAM_shifted", level_and_crop=True)
    assert report.codes == [policy.SHIFTED_CAMERA]
    assert "level-and-crop does not apply: a horizontal shift" in report[0]


# --------------------------------------------------------------------------- Send on the fake


def _to4(rows) -> np.ndarray:
    m = np.zeros((4, 4))
    for i in range(3):
        m[i, :3] = rows[i]
    m[3, :3] = rows[3]
    m[3, 3] = 1.0
    return m


def _rows(matrix) -> list[tuple[float, float, float]]:
    return [tuple(float(v) for v in getattr(matrix, f"row{i}")) for i in (1, 2, 3, 4)]


class LinkedCamera(FakeCamera):
    """objecttransform = object offset . transform, read-only after construction (as in Max). The
    offset is FakeCamera's pivot decoy: transform sits 500 units off the object."""

    def __init__(self, *args, **kw):
        object.__setattr__(self, "_offset", None)
        object.__setattr__(self, "_object_tm", None)
        super().__init__(*args, **kw)
        self._offset = _to4(_rows(self._object_tm)) @ np.linalg.inv(_to4(_rows(self.transform)))

    @property
    def objecttransform(self):
        if self._offset is None:
            return self._object_tm
        m = self._offset @ _to4(_rows(self.transform))
        return FakeMatrix3(tuple(tuple(m[i, :3]) for i in range(4)))

    @objecttransform.setter
    def objecttransform(self, value):
        if self._offset is not None:
            raise AttributeError("objecttransform is read-only: set transform")
        self._object_tm = value


def teach_copy(rt) -> list[Any]:
    """rt.copy (a clone registered as a camera), rt.delete, rt.matrix3 and rt.Point3 on the fake."""
    made: list[Any] = []

    def copy_node(node):
        clone = copy.copy(node)
        clone.properties = dict(node.properties)
        clone.name = f"{node.name}001"
        rt.cameras[clone.name] = clone
        rt.objects.append(clone)
        made.append(clone)
        return clone

    def delete(node):
        for key, value in list(rt.cameras.items()):
            if value is node:
                del rt.cameras[key]
        rt.objects = [n for n in rt.objects if n is not node]

    rt.copy, rt.delete = copy_node, delete
    rt.matrix3 = lambda *rows: FakeMatrix3(tuple(tuple(r) for r in rows))
    rt.Point3 = FakePoint3
    return made


@pytest.fixture
def daemon_env(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def two_point_scene(install_scene, pitch=8.0, size=(160, 90), cls=LinkedCamera, **props):
    scene = install_scene("clubhouse")
    scene.renderWidth, scene.renderHeight = size
    scene.maxVersion = lambda: [28000, 63, 0, 28, 2, 0, 2201, 2026, ".2 Update"]
    gamma_colour(scene)
    vray = FakeVRay(scene)
    props = props or {"auto_vertical_tilt": True}
    node = add_camera(scene, "CAM_tilt", pose_rows(YAW, pitch), cls=cls, **props)
    return scene, vray, node, teach_copy(scene)


def send(cameras, packets, cfg, **options):
    opts = {"config": cfg, "project_id": "tula-wot", "staging_dir": None, **options}
    return send_packet.run(cameras, packets, cfg["daemon_url"], opts)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_tilt_corrected_camera_sends_a_level_and_crop_packet(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, vray, node, made = two_point_scene(install_scene)
    before = _rows(node.objecttransform)
    [result] = send(["CAM_tilt"], packets, cfg)
    assert result.ok, (result.stage, result.errors)
    assert not [p for p in result.problems if p["code"] == policy.SHIFTED_CAMERA]
    assert any(w.startswith("SHIFTED_CAMERA: camera CAM_tilt uses") for w in result.warnings)
    root = Path(result.packet_dir)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []

    fx = FOCAL * 160 / FILM
    dy = fx * math.tan(math.radians(8.0))
    pad = math.ceil(dy)
    cam = read_json(root / "manifest.json")["camera"]
    assert cam["name"] == "CAM_tilt" and cam["resolution"]["width"] == 160
    assert cam["resolution"]["height"] == 90 + 2 * pad
    assert cam["safe_frame"] == {"x": 0, "y": round(pad - dy), "width": 160, "height": 90}
    assert cam["overscan"]["engine_aspect"] == send_packet._ratio(160, 90 + 2 * pad)
    assert cam["overscan"]["note"].startswith(
        "level-and-crop from a tilt-corrected camera: pitch 8.000 deg, dy "
    )
    assert cam["lens_shift"]["vertical"] == 0 and cam["lens_shift"]["vertical_tilt_correction"] is False
    assert cam["intrinsics_px"]["fx"] == pytest.approx(fx, rel=1e-9)
    assert cam["intrinsics_px"]["principal_point"] == [80.0, (90 + 2 * pad) / 2]
    assert np.allclose(
        cam["world_from_camera"], plate_spec.world_from_camera_from_max_rows(pose_rows(YAW, 0.0))
    )
    # the packet's camera minus its safe frame is the tilt-corrected camera, within half a pixel
    assert_crop_identity(tilt_corrected(YAW, 8.0, fx, 160, 90), cam, cam["safe_frame"], 160, 90)

    # V-Ray rendered the temporary level copy at the taller size; the copy is gone, the camera untouched
    call = scene.render_calls[-1]
    assert call["camera"] is made[0] and made[0].name == camera_level.LEVEL_PREFIX + "CAM_tilt"
    assert (call["outputwidth"], call["outputheight"]) == (160, 90 + 2 * pad)
    assert made[0] not in list(scene.cameras) and made[0] not in scene.objects
    assert node.auto_vertical_tilt is True and _rows(node.objecttransform) == before
    assert (scene.renderWidth, scene.renderHeight) == (160, 90)
    assert result.frame["level"]["pad_px"] == pad and result.frame["height"] == 90 + 2 * pad
    journal = read_json(Path(result.journal))
    assert any(s["step"] == "level" for s in journal["steps"])


def test_level_and_crop_false_keeps_the_refusal(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _, made = two_point_scene(install_scene)
    for opts in ({"recipes": {"CAM_tilt": {"level_and_crop": False}}}, {"level_and_crop": False}):
        [result] = send(["CAM_tilt"], packets, cfg, **opts)
        assert not result.ok and result.stage == "lint"
        assert policy.SHIFTED_CAMERA in [p["code"] for p in result.problems]
    assert scene.render_calls == [] and made == [] and list(packets.iterdir()) == []


def test_a_recipe_object_with_the_field_is_read():
    class Recipe:
        level_and_crop = False

    assert send_packet.level_and_crop_on("C", Recipe(), {}) is False
    assert send_packet.level_and_crop_on("C", CameraRecipe(camera="C"), {}) is True
    assert send_packet.level_and_crop_on("C", None, {"recipes": {"C": {"level_and_crop": 0}}}) is False


def test_level_and_crop_composes_with_an_overscan_aspect(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, vray, node, made = two_point_scene(install_scene, pitch=3.55)
    fx = FOCAL * 160 / FILM
    dy = fx * math.tan(math.radians(3.55))
    pad = math.ceil(dy)
    recipes = {"CAM_tilt": CameraRecipe(camera="CAM_tilt", overscan_aspect="16:9")}
    [result] = send(["CAM_tilt"], packets, cfg, recipes=recipes)
    assert result.ok, (result.stage, result.errors)
    cam = read_json(Path(result.packet_dir) / "manifest.json")["camera"]
    tall = 90 + 2 * pad  # 160 x 104: taller than 16:9, so the overscan adds width (fov widened)
    width = round(tall * 16 / 9)
    assert (cam["resolution"]["width"], cam["resolution"]["height"]) == (width, tall)
    safe = cam["safe_frame"]
    assert safe == {"x": (width - 160) // 2, "y": round(pad - dy), "width": 160, "height": 90}
    assert cam["overscan"]["engine_aspect"] == "16:9"
    assert cam["overscan"]["note"].startswith("level-and-crop from a tilt-corrected camera: pitch 3.550 deg")
    assert "rendered inside" in cam["overscan"]["note"]
    assert cam["intrinsics_px"]["fx"] == pytest.approx(fx, rel=1e-9)
    assert vray.film_during_render == [pytest.approx(FILM * width / 160)]  # the copy was widened
    assert node.film_width == FILM
    assert_crop_identity(tilt_corrected(YAW, 3.55, fx, 160, 90), cam, safe, 160, 90)
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_a_vertically_shifted_camera_sends_level_and_crop(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, node, made = two_point_scene(install_scene, pitch=0.0, vertical_shift=0.1)
    [result] = send(["CAM_tilt"], packets, cfg)
    assert result.ok, (result.stage, result.errors)
    cam = read_json(Path(result.packet_dir) / "manifest.json")["camera"]
    assert cam["resolution"]["height"] == 90 + 2 * 9 and cam["safe_frame"]["y"] == 0
    assert "vertically shifted camera: shift 0.1" in cam["overscan"]["note"]
    assert node.vertical_shift == 0.1 and made[0].vertical_shift == 0.0
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_a_copy_whose_pose_does_not_take_stops_the_send_and_is_deleted(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    # a plain FakeCamera: setting transform does not move objecttransform (an animated or constrained
    # camera in Max would behave like this)
    scene, _, node, made = two_point_scene(install_scene, cls=FakeCamera)
    [result] = send(["CAM_tilt"], packets, cfg)
    assert not result.ok and result.stage == "render"
    assert "level-and-crop" in result.errors[0] and "did not take" in result.errors[0]
    assert scene.render_calls == [] and made and made[0] not in scene.objects


def test_a_max_without_copy_stops_with_the_reason(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _, _ = two_point_scene(install_scene)
    del scene.copy
    [result] = send(["CAM_tilt"], packets, cfg)
    assert not result.ok and result.stage == "render" and "no copy()" in result.errors[0]


def test_a_too_steep_two_point_camera_stays_refused(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, node, made = two_point_scene(install_scene, pitch=50.0)
    [result] = send(["CAM_tilt"], packets, cfg)
    assert not result.ok and result.stage == "lint" and "limit" in result.errors[0]
    assert scene.render_calls == [] and made == []


def test_the_hero_frame_recheck_refuses_what_lint_did_not_see(scene):
    add_camera(scene, "CAM_x", pose_rows(YAW, 50.0), auto_vertical_tilt=True)
    with pytest.raises(send_packet.SendAbort) as info:
        send_packet._level_plan("CAM_x", digest.camera_digest("CAM_x"), W, H, 7)
    assert info.value.stage == "lint" and info.value.problems[0]["code"] == policy.SHIFTED_CAMERA
    assert "at frame 7" in info.value.reason and "limit" in info.value.reason
    assert send_packet._level_plan("CAM_main", digest.camera_digest("CAM_main"), W, H, 0) is None


def test_the_level_copy_reads_as_a_v01_pinhole(scene):
    add_camera(scene, "CAM_tilt", pose_rows(YAW, 3.55), cls=LinkedCamera, auto_vertical_tilt=True)
    teach_copy(scene)
    level = camera_level.plan(digest.camera_digest("CAM_tilt"), W, H)
    scene.renderWidth, scene.renderHeight = W, level.render_height
    overscan = {"engine_aspect": send_packet._ratio(W, level.render_height), "note": level.note}
    with camera_level.levelled(scene.cameras["CAM_tilt"], level) as clone:
        cam = digest.camera_digest(clone.name, recipe={"safe_frame": level.safe_frame, "overscan": overscan})
    block = packet.build_camera_block(cam)
    assert block["safe_frame"] == level.safe_frame and block["lens_shift"]["vertical"] == 0
    assert block["overscan"] == overscan and block["resolution"]["height"] == level.render_height
    assert clone not in scene.objects and "CAM_tilt" in scene.cameras
    assert scene.cameras["CAM_tilt"].auto_vertical_tilt is True
