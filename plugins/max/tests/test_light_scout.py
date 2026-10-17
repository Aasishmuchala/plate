"""actions.light (the MaxGaffer wrapper, re-render with supersedes, Vantage live link), scout_poses
(the pure geometry copied from MaxDirector) and actions.scout (temporary cameras, previews, optional
packets) on the fake scene. MaxGaffer is a fake maxgaffer.api module put into sys.modules; the
end-to-end runs reuse test_send_packet's simulated V-Ray and stub daemon (imported helpers only)."""

from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fake_pymxs import FakeCamera, FakeMatrix3, FakePoint3

import plate_spec
from plate_max import scout_poses
from plate_max.actions import light, scout, send_packet
from plate_max.packet_id import new_packet_id

pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, FakeVRay, gamma_colour, serve_daemon  # noqa: E402

MAXDIRECTOR = Path.home() / "Desktop" / "maxdirector"


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


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def small_clubhouse(install_scene, size=(160, 90)):
    scene = install_scene("clubhouse")
    scene.renderWidth, scene.renderHeight = size
    scene.maxVersion = lambda: [28000, 63, 0, 28, 2, 0, 2201, 2026, ".2 Update"]
    gamma_colour(scene)
    return scene, FakeVRay(scene)


# --------------------------------------------------------------------------- a fake MaxGaffer


class FakeGaffer:
    """maxgaffer.api's two calls PLATE uses, recording their arguments."""

    def __init__(self):
        self.match_calls: list[tuple[tuple, dict]] = []
        self.apply_calls: list[str] = []
        self.states: set[str] = set()
        self.raise_on_match: Exception | None = None

    def match_camera(self, camera_name, reference_path="", log=None, should_cancel=None, **kw):
        self.match_calls.append(
            (
                (camera_name,),
                {"reference_path": reference_path, "log": log, "should_cancel": should_cancel, **kw},
            )
        )
        if self.raise_on_match:
            raise self.raise_on_match
        log("matched")
        self.states.add(camera_name)
        return {
            "score": 97.4,
            "stop_reason": "target",
            "iterations": 6,
            "state": {"ev": 12.5, "kelvin": 5600},
            "run_dir": "runs/1",
            "renders": ["r1.png"],
            "polish_gain": 0.8,
            "ceiling_converged": False,
        }

    def apply_camera_state(self, camera_name):
        self.apply_calls.append(camera_name)
        if camera_name not in self.states:
            raise RuntimeError(f"no saved lighting state for camera '{camera_name}'")
        return ["dome texture restored"]


@pytest.fixture
def gaffer(monkeypatch):
    fake = FakeGaffer()
    pkg = types.ModuleType("maxgaffer")
    api = types.ModuleType("maxgaffer.api")
    api.match_camera = fake.match_camera
    api.apply_camera_state = fake.apply_camera_state
    pkg.api = api
    monkeypatch.setitem(sys.modules, "maxgaffer", pkg)
    monkeypatch.setitem(sys.modules, "maxgaffer.api", api)
    return fake


@pytest.fixture
def no_gaffer(monkeypatch):
    monkeypatch.setitem(sys.modules, "maxgaffer", None)  # import maxgaffer raises ImportError
    monkeypatch.setitem(sys.modules, "maxgaffer.api", None)


# --------------------------------------------------------------------------- Light


def test_match_forwards_the_kwargs_to_maxgaffer(gaffer, tmp_path):
    ref = tmp_path / "golden_hour.jpg"
    ref.write_bytes(b"\xff\xd8 reference")
    cancel = lambda: False  # noqa: E731
    out = light.match("CAM_front", str(ref), locks={"sun_azimuth"}, deep=True, should_cancel=cancel)
    assert out["ok"] is True and out["score"] == 97.4 and out["camera"] == "CAM_front"
    [(args, kwargs)] = gaffer.match_calls
    assert args == ("CAM_front",)
    assert kwargs["reference_path"] == str(ref) and kwargs["locks"] == {"sun_azimuth"}
    assert kwargs["sweep"] is True and kwargs["deep"] is True and kwargs["should_cancel"] is cancel
    assert callable(kwargs["log"])
    assert set(kwargs) == {"reference_path", "log", "should_cancel", "locks", "sweep", "deep"}
    # defaults: no locks, not deep, a no-op log and a never-cancel
    light.match("CAM_entry", "")
    _, kw = gaffer.match_calls[-1]
    assert kw["locks"] is None and kw["deep"] is False and kw["should_cancel"]() is False


def test_match_failures_are_data(gaffer, tmp_path):
    assert light.match("CAM_front", str(tmp_path / "missing.jpg"))["code"] == light.NO_REFERENCE
    gaffer.raise_on_match = ValueError("no VRaySun or dome light")
    out = light.match("CAM_front", "")
    assert out["ok"] is False and out["code"] == light.MAXGAFFER_FAILED and "VRaySun" in out["error"]


def test_apply_forwards_and_reports_a_missing_state(gaffer):
    missing = light.apply("CAM_front")
    assert missing == {
        "ok": False,
        "camera": "CAM_front",
        "code": light.NO_SAVED_STATE,
        "error": "no saved lighting state for camera 'CAM_front'",
    }
    light.match("CAM_front", "")
    assert light.apply("CAM_front") == {
        "ok": True,
        "camera": "CAM_front",
        "warnings": ["dome texture restored"],
    }
    assert gaffer.apply_calls == ["CAM_front", "CAM_front"]


def test_without_maxgaffer_everything_reports_maxgaffer_missing(no_gaffer, monkeypatch):
    ok, why = light.available()
    assert ok is False and why.startswith(light.MAXGAFFER_MISSING)
    assert light.maxgaffer_api() is None
    assert light.match("CAM_front", "")["code"] == light.MAXGAFFER_MISSING
    assert light.apply("CAM_front")["code"] == light.MAXGAFFER_MISSING

    def never(*args, **kwargs):
        raise AssertionError("no packet may be rendered without MaxGaffer")

    monkeypatch.setattr(send_packet, "run", never)
    results = light.run(["CAM_front", "CAM_entry"], "unused")
    assert [(r.camera, r.ok, r.code) for r in results] == [
        ("CAM_front", False, light.MAXGAFFER_MISSING),
        ("CAM_entry", False, light.MAXGAFFER_MISSING),
    ]
    assert results[0].as_dict()["errors"] == [why]


def test_a_maxgaffer_without_the_api_calls_is_missing(monkeypatch):
    api = types.ModuleType("maxgaffer.api")
    api.match_camera = lambda *a, **k: {}
    monkeypatch.setitem(sys.modules, "maxgaffer", types.ModuleType("maxgaffer"))
    monkeypatch.setitem(sys.modules, "maxgaffer.api", api)
    ok, why = light.available()
    assert not ok and "apply_camera_state" in why


def _still(packets: Path, camera: str, kind: str = "still", ms: int = 1_760_000_000_000) -> str:
    pid = new_packet_id(now_ms=ms)
    (packets / pid).mkdir(parents=True)
    manifest = {"packet_id": pid, "kind": kind, "camera": {"name": camera}}
    (packets / pid / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    return pid


def test_rerender_supersedes_the_newest_still_of_the_camera(monkeypatch, tmp_path):
    packets = tmp_path / "packets"
    older = _still(packets, "CAM_front", ms=1_760_000_000_000)
    newer = _still(packets, "CAM_front", ms=1_760_000_005_000)
    _still(packets, "CAM_entry", ms=1_760_000_009_000)
    _still(packets, "CAM_front", kind="motion", ms=1_760_000_009_000)
    (packets / "stray").mkdir()
    (packets / "stray" / "manifest.json").write_text("{broken", encoding="utf-8")
    assert light.latest_packet(packets, "CAM_front") == newer > older
    assert light.latest_packet(packets, "CAM_nobody") is None
    seen: list[tuple] = []

    def fake_run(cameras, out_root, daemon_base, options):
        seen.append((cameras, out_root, daemon_base, dict(options)))
        return [send_packet.SendResult(camera=cameras[0], ok=True, packet_id="pkt_new")]

    monkeypatch.setattr(send_packet, "run", fake_run)
    sent = light.rerender_packet("CAM_front", packets, "http://127.0.0.1:4610", {"config": {"token": "t"}})
    assert sent.ok and sent.packet_id == "pkt_new"
    cameras, out_root, base, opts = seen[-1]
    assert cameras == ["CAM_front"] and out_root == packets and base == "http://127.0.0.1:4610"
    assert opts["supersedes"] == {"CAM_front": newer} and opts["config"] == {"token": "t"}
    light.rerender_packet("CAM_front", packets, supersedes=older)
    assert seen[-1][3]["supersedes"] == {"CAM_front": older}
    light.rerender_packet("CAM_nobody", packets)
    assert "supersedes" not in seen[-1][3]


def test_light_end_to_end_relights_and_supersedes_the_old_packet(install_scene, daemon_env, gaffer, tmp_path):
    daemon, cfg, packets = daemon_env
    small_clubhouse(install_scene)
    [hero] = send_packet.run(["CAM_front"], packets, cfg["daemon_url"], {"config": cfg})
    assert hero.ok, hero.errors
    ref = tmp_path / "dusk.png"
    ref.write_bytes(b"\x89PNG reference")
    progress: list[tuple] = []
    [result] = light.run(
        ["CAM_front"],
        packets,
        cfg["daemon_url"],
        {
            "config": cfg,
            "references": {"CAM_front": str(ref)},
            "deep": True,
            "progress": lambda *a: progress.append(a),
        },
    )
    assert result.ok, (result.stage, result.code, result.errors)
    assert result.supersedes == hero.packet_id and result.packet_id not in (None, hero.packet_id)
    assert gaffer.match_calls[0][1]["reference_path"] == str(ref) and gaffer.match_calls[0][1]["deep"] is True
    assert gaffer.apply_calls == ["CAM_front"] and "dome texture restored" in result.warnings
    root = packets / result.packet_id
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    assert read_json(root / "manifest.json")["supersedes"] == hero.packet_id
    assert len(daemon.posted) == 2 and progress[-1][:2] == (1, 1) and "relit" in progress[-1][2]
    assert result.as_dict()["match"]["score"] == 97.4


def test_light_without_rerender_stops_after_apply(gaffer, monkeypatch):
    monkeypatch.setattr(send_packet, "run", lambda *a, **k: pytest.fail("no re-render was asked for"))
    [result] = light.run("CAM_front", "unused", options={"rerender": False})
    assert result.ok and result.stage == "done" and result.packet_id is None


def test_a_failed_match_stops_that_camera_only(gaffer, install_scene, daemon_env):
    _, cfg, packets = daemon_env
    small_clubhouse(install_scene)
    calls = {"n": 0}
    real = gaffer.match_camera

    def flaky(camera_name, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("V-Ray: render cancelled")
        return real(camera_name, **kw)

    sys.modules["maxgaffer.api"].match_camera = flaky
    results = light.run(["CAM_front", "CAM_entry"], packets, cfg["daemon_url"], {"config": cfg})
    assert [r.ok for r in results] == [False, True]
    assert results[0].code == light.MAXGAFFER_FAILED and results[0].stage == "match"
    assert results[1].supersedes is None  # CAM_entry had no packet yet: a first packet, nothing replaced


def test_vantage_preview_sets_the_viewport_camera_and_starts_the_link(scene):
    seen: list[Any] = []
    scene.viewport = SimpleNamespace(setCamera=seen.append, getCamera=lambda: None)
    out = light.vantage_preview("CAM_main")
    assert out["ok"] is True and "vantageStartLiveLink()" in out["how"] and out["warnings"] == []
    assert [c.name for c in seen] == ["CAM_main"] and scene.executed == ["vantageStartLiveLink()"]
    assert light.vantage_preview("CAM_nowhere")["code"] == light.CAMERA_MISSING


def test_vantage_preview_reports_a_missing_live_link(scene):
    def refuse(script):
        raise RuntimeError(f"-- Type error: Call needs function or class, got: undefined ({script})")

    scene.execute = refuse
    out = light.vantage_preview("CAM_main")
    assert out["ok"] is False and out["code"] == light.LIVE_LINK_FAILED and "V-Ray menu" in out["error"]
    assert any("viewport" in w for w in out["warnings"])


# --------------------------------------------------------------------------- scout poses (pure)

ROOM = ((0.0, 0.0, 0.0), (5000.0, 4000.0, 3000.0))  # a 5 x 4 x 3 m room in millimetres


def test_scout_poses_cover_the_room():
    views = scout_poses.scout_poses(ROOM, "Z", 1000.0, 1.6)
    assert [(v.id, v.label) for v in views] == [
        (0, "corner_0"),
        (1, "corner_1"),
        (2, "corner_2"),
        (3, "corner_3"),
        (4, "high_3q"),
        (5, "plan"),
    ]
    for v in views[:4]:
        assert v.pose.pos[2] == pytest.approx(1600.0) and v.pose.look_at == (2500.0, 2000.0, 1600.0)
        assert v.pose.fov_mm == 24.0
    assert views[0].pose.pos[:2] == pytest.approx((200.0, 160.0))  # inset 8% toward the centre
    diag = math.sqrt(5000**2 + 4000**2 + 3000**2)
    assert views[4].pose.pos[2] == pytest.approx(1600.0 + 0.4 * diag) and views[4].pose.fov_mm == 28.0
    assert views[5].pose.pos == pytest.approx((2500.0, 2000.0, 3000.0 + 0.6 * diag))
    assert views[5].pose.up == (0.0, 1.0, 0.0)


def test_feature_views_aim_at_the_tallest_node():
    wall = {"name": "ARCH_window_wall", "bbox": ((0.0, 3800.0, 0.0), (5000.0, 4000.0, 2800.0))}
    floor = {"name": "HARD_floor", "bbox": ((0.0, 0.0, 0.0), (5000.0, 4000.0, 20.0))}
    lamp = {"name": "LGT_lamp", "bbox": ((0, 0, 0), (1, 1, 2900)), "category": "light"}
    views = scout_poses.scout_poses(ROOM, "Z", 1000.0, 1.6, [floor, wall, lamp])
    low, high = views[6], views[7]
    assert (low.label, high.label) == ("feature_low", "feature_high")
    assert low.pose.look_at == (2500.0, 3900.0, 1400.0) and low.pose.fov_mm == 35.0
    assert low.pose.pos[2] == pytest.approx(1100.0) and high.pose.pos[2] == pytest.approx(2300.0)
    assert low.pose.pos[1] < 3900.0  # standing in the room, looking at the wall


def test_scout_poses_match_maxdirector_when_it_is_on_this_machine(monkeypatch):
    if not (MAXDIRECTOR / "maxdirector" / "core" / "scout.py").is_file():
        pytest.skip("MaxDirector is not on this machine")
    monkeypatch.syspath_prepend(str(MAXDIRECTOR))
    try:
        from maxdirector.core import scout as md_scout
        from maxdirector.core.models import BBox, Category, NodeInfo, UpAxis
    except Exception as exc:  # noqa: BLE001 an incompatible checkout is not this repo's failure
        pytest.skip(f"MaxDirector does not import here: {exc}")
    wall = NodeInfo(1, "wall", "Box", Category.GEOMETRY, BBox((0.0, 3800.0, 0.0), (5000.0, 4000.0, 2800.0)))
    theirs = md_scout.scout_poses(BBox(*ROOM), UpAxis.Z, 1000.0, 1.6, [wall])
    ours = scout_poses.scout_poses(ROOM, "Z", 1000.0, 1.6, [wall])
    assert [(v.id, v.label) for v in ours] == [(v.id, v.label) for v in theirs]
    for a, b in zip(ours, theirs, strict=True):
        assert a.pose.pos == pytest.approx(tuple(b.pose.pos)) and a.pose.look_at == pytest.approx(
            tuple(b.pose.look_at)
        )
        assert a.pose.fov_mm == b.pose.fov_mm and a.pose.up == pytest.approx(tuple(b.pose.up))


def test_look_at_rows_aim_down_minus_z():
    x, y, z, p = scout_poses.look_at_rows((0.0, -5000.0, 1600.0), (0.0, 0.0, 1600.0))
    assert (
        z == pytest.approx((0.0, -1.0, 0.0))
        and x == pytest.approx((1.0, 0.0, 0.0))
        and y == pytest.approx((0.0, 0.0, 1.0))
    )
    assert p == (0.0, -5000.0, 1600.0)
    # straight down with a Z up vector picks a stable right vector
    x, y, z, _ = scout_poses.look_at_rows((0.0, 0.0, 9000.0), (0.0, 0.0, 0.0))
    assert z == pytest.approx((0.0, 0.0, 1.0)) and abs(sum(a * b for a, b in zip(x, z, strict=True))) < 1e-9
    with pytest.raises(ValueError):
        scout_poses.look_at_rows((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))


# --------------------------------------------------------------------------- Scout on the fake scene


class ScoutCamera(FakeCamera):
    """A freshly created free camera: its transform is its objecttransform (no pivot offset)."""

    @property
    def transform(self):
        return self.objecttransform

    @transform.setter
    def transform(self, value):
        self.objecttransform = value

    @property
    def fov(self):  # derived from the lens, as on a V-Ray Physical Camera without specify_fov
        return math.degrees(2 * math.atan(self.film_width / (2 * self.focal_length * self.zoom_factor)))

    @fov.setter
    def fov(self, value):
        pass


def scout_scene(install_scene, size=(3840, 2160)):
    """The clubhouse with world bounding boxes on its nodes and the MAXScript calls Scout makes
    (camera creation, delete, matrix3, Point3) taught to the fake. Returns (scene, log)."""
    scene = install_scene("clubhouse")
    scene.renderWidth, scene.renderHeight = size
    for i, node in enumerate(n for n in scene.objects if scene.superClassOf(n) == "GeometryClass"):
        node.properties["min"] = FakePoint3(-20000.0 + 100 * i, -15000.0, 0.0)
        node.properties["max"] = FakePoint3(20000.0, 15000.0, 3000.0 + 10 * i)
    log: dict[str, list] = {"created": [], "deleted": [], "renders": []}

    def make_camera():
        cam = ScoutCamera(f"VRayPhysCamera{len(log['created']) + 1:03d}")
        cam.targeted = True
        scene.objects.append(cam)
        log["created"].append(cam)
        return cam

    def delete(node):
        log["deleted"].append(node.name)
        scene.objects.remove(node)

    plain = scene.render

    def render(*args, **kw):
        cam = kw["camera"]
        vr = scene.renderers.current
        log["renders"].append(
            {
                "camera": cam.name,
                "rows": [tuple(r) for r in cam.objecttransform.rows],
                "focal": cam.focal_length,
                "targeted": cam.targeted,
                "raw": getattr(vr, "output_saveRawFile", None),
                "split": getattr(vr, "output_splitgbuffer", None),
                **{k: kw.get(k) for k in ("outputwidth", "outputheight", "outputfile", "vfb")},
            }
        )
        return plain(*args, **kw)

    scene.VRayPhysicalCamera = make_camera
    scene.delete = delete
    scene.matrix3 = lambda *rows: FakeMatrix3(tuple(tuple(r) for r in rows))
    scene.Point3 = FakePoint3
    scene.render = render
    vr = scene.renderers.current
    vr.output_saveRawFile, vr.output_splitgbuffer = True, True  # the artist's own outputs
    return scene, log


def scout_cameras(scene) -> list[str]:
    return [n.name for n in scene.objects if n.name.startswith(scout.SCOUT_PREFIX)]


def test_scout_creates_previews_and_removes_its_temp_cameras(install_scene, tmp_path):
    scene, log = scout_scene(install_scene)
    progress: list[tuple] = []
    results = scout.run(
        options={"preview_dir": str(tmp_path / "scouts"), "progress": lambda *a: progress.append(a)}
    )
    n = len(results)
    assert n == 8 and all(r.ok for r in results), [r.errors for r in results]
    assert [r.camera for r in results] == [f"__PLATE_scout_{i}" for i in range(n)]
    assert [r["camera"] for r in log["renders"]] == [r.camera for r in results]
    for r, call in zip(results, log["renders"], strict=True):
        assert Path(r.preview).is_file() and Path(r.preview).name == f"scout_{r.view_id}.png"
        assert (call["outputwidth"], call["outputheight"]) == (1280, 720) and call["vfb"] is False
        assert call["raw"] is False and call["split"] is False  # the artist's output files untouched
        assert call["targeted"] is False and call["focal"] == r.pose["fov_mm"]
        x, y, z, p = scout_poses.look_at_rows(
            tuple(r.pose["pos"]), tuple(r.pose["look_at"]), tuple(r.pose["up"])
        )
        assert call["rows"][3] == pytest.approx(p) and call["rows"][2] == pytest.approx(z)
    # every temporary camera is gone, the artist's settings are back
    assert scout_cameras(scene) == [] and sorted(log["deleted"]) == sorted(r.camera for r in results)
    assert len(log["created"]) == n
    vr = scene.renderers.current
    assert vr.output_saveRawFile is True and vr.output_splitgbuffer is True
    assert progress[-1][:2] == (n, n) and not any(r.kept or r.packet_id for r in results)


def test_scout_keep_leaves_the_cameras_and_a_stale_scout_is_replaced(install_scene, tmp_path):
    scene, log = scout_scene(install_scene)
    stale = ScoutCamera("__PLATE_scout_0")
    scene.objects.append(stale)
    results = scout.run(options={"preview_dir": str(tmp_path), "keep": True, "views": [0, 5]})
    assert [r.view_id for r in results] == [0, 5] and all(r.ok and r.kept for r in results)
    assert log["deleted"] == ["__PLATE_scout_0"]  # only the stale one, before its replacement
    assert sorted(scout_cameras(scene)) == ["__PLATE_scout_0", "__PLATE_scout_5"]
    assert stale not in scene.objects


def test_scout_makes_packets_only_when_asked(install_scene, monkeypatch, tmp_path):
    scout_scene(install_scene)
    monkeypatch.setattr(send_packet, "run", lambda *a, **k: pytest.fail("a scout packet needs packets=True"))
    results = scout.run(tmp_path / "packets", options={"preview_dir": str(tmp_path / "p"), "views": [1]})
    assert results[0].ok and results[0].packet_id is None
    seen: list[tuple] = []

    def fake_run(cameras, out_root, daemon_base, options):
        seen.append((list(cameras), out_root, dict(options)))
        return [send_packet.SendResult(camera=c, ok=True, packet_id=f"pkt_{c}") for c in cameras]

    monkeypatch.setattr(send_packet, "run", fake_run)
    results = scout.run(
        tmp_path / "packets",
        "http://127.0.0.1:4610",
        {"preview_dir": str(tmp_path / "p"), "views": [1, 4], "packets": True},
    )
    assert [r.packet_id for r in results] == ["pkt___PLATE_scout_1", "pkt___PLATE_scout_4"]
    assert seen[0][0] == ["__PLATE_scout_1", "__PLATE_scout_4"] and seen[0][1] == tmp_path / "packets"
    assert "packets" not in seen[0][2] and "views" not in seen[0][2]


def test_scout_packet_end_to_end_is_a_real_packet(install_scene, daemon_env, tmp_path):
    _, cfg, packets = daemon_env
    scene, log = scout_scene(install_scene, size=(160, 90))
    scene.maxVersion = lambda: [28000, 63, 0, 28, 2, 0, 2201, 2026, ".2 Update"]
    gamma_colour(scene)
    FakeVRay(scene)
    [result] = scout.run(
        packets,
        cfg["daemon_url"],
        {
            "preview_dir": str(tmp_path / "p"),
            "views": [4],
            "packets": True,
            "config": cfg,
            "project_id": "tula-wot",
        },
    )
    assert result.ok, result.errors
    root = Path(result.packet_dir)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    manifest = read_json(root / "manifest.json")
    assert manifest["camera"]["name"] == "__PLATE_scout_4" and manifest["shot_id"] == "plate-scout-4"
    assert manifest["camera"]["focal_length_mm"] == 28.0
    assert scout_cameras(scene) == []


def test_a_scene_without_geometry_reports_it(scene):
    scene.objects = []
    [result] = scout.run(options={"preview_dir": "unused"})
    assert not result.ok and result.code == scout.NO_GEOMETRY


def test_a_camera_that_cannot_be_created_is_reported_per_view(install_scene, tmp_path):
    scene, log = scout_scene(install_scene)
    del scene.VRayPhysicalCamera
    results = scout.run(options={"preview_dir": str(tmp_path), "views": [0, 1]})
    assert [r.code for r in results] == [scout.CAMERA_FAILED, scout.CAMERA_FAILED]
    assert log["renders"] == [] and scout_cameras(scene) == []
