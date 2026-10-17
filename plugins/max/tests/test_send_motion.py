"""bridge.motion and actions.send_motion on the fake scene: a hero still is sent first (test_send_packet's
simulated V-Ray and stub daemon, imported helpers only so no test is collected twice), the camera is
animated, and Send Motion writes a motion packet that plate_spec.validate_packet_dir accepts with
every BLAKE3 from the stub daemon's /hash.

SequenceVRay extends the simulated V-Ray with what a clay sequence needs: the split render-channel
output (one float EXR per enabled render element, written with OpenEXR from the dev venv) and a
16-bit PNG from the VFB save, and it records the material override and the enabled elements at
every render so the clay override is checked as it was during the render, not after.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from fake_pymxs import FakeColor, FakeMaterial, FakeMatrix3

import plate_spec
from plate_max.actions import send_motion, send_packet
from plate_max.bridge import motion

np = pytest.importorskip("numpy")
OpenEXR = pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, FakeVRay, gamma_colour, serve_daemon  # noqa: E402

SEQ = (64, 36)  # the 160x90 hero's exact aspect, small enough to keep the EXRs quick


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


@pytest.fixture(autouse=True)
def _no_clay_left():
    yield
    motion.clay_override(False)


@pytest.fixture
def daemon_env(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- simulated V-Ray, sequence side


def png16(width: int, height: int) -> bytes:
    """A valid 16-bit RGB PNG (zlib + struct, no Pillow)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + struct.pack(">HHH", 46000, 46000, 46000) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def clay_material_factory(scene):
    made: list[FakeMaterial] = []

    def factory(**kw):
        mat = FakeMaterial("Material #1", "VRayMtl")
        mat.diffuse = FakeColor(128, 128, 128)
        mat.reflection = FakeColor(40, 40, 40)
        made.append(mat)
        return mat

    scene.VRayMtl = factory
    scene.Color = FakeColor
    return made


class SequenceVRay:
    """Split render channels and the VFB PNG on top of FakeVRay. Every render is logged with the
    override switch, material and exclude list and the enabled element layers at that moment."""

    def __init__(self, scene, frame_suffix: bool = False, png_bits: int = 16):
        self.scene = scene
        self.vr = scene.renderers.current
        self.frame_suffix = frame_suffix
        self.png_bits = png_bits
        self.renders: list[dict[str, Any]] = []
        self.size = (scene.renderWidth, scene.renderHeight)
        self.inner_render, self.inner_vfb = scene.render, scene.vfbControl
        scene.render, scene.vfbControl = self.render, self.vfb

    def _override(self) -> dict[str, Any]:
        vr = self.vr
        on = next((getattr(vr, n) for n in motion.OVERRIDE_ON if hasattr(vr, n)), None)
        mtl = next((getattr(vr, n) for n in motion.OVERRIDE_MTL if hasattr(vr, n)), None)
        exclude = next((getattr(vr, n) for n in motion.OVERRIDE_EXCLUDE if hasattr(vr, n)), None)
        return {
            "on": on,
            "material": getattr(mtl, "name", None),
            "diffuse": getattr(mtl, "diffuse", None),
            "exclude": sorted(n.name for n in exclude or []),
        }

    def render(self, *args, **kw):
        bitmap = self.inner_render(*args, **kw)
        w, h = int(kw["outputwidth"]), int(kw["outputheight"])
        self.size = (w, h)
        enabled = [e for e in self.scene.element_manager.elements if e.enabled]
        self.renders.append(
            {
                "frame": kw.get("frame"),
                "size": (w, h),
                "elements": sorted(e.elementname for e in enabled),
                "split": bool(getattr(self.vr, "output_splitgbuffer", False)),
                "raw": bool(getattr(self.vr, "output_saveRawFile", False)),
                **self._override(),
            }
        )
        target = getattr(self.vr, "output_splitfilename", "")
        if self.vr.output_splitgbuffer and target:
            stem = Path(target)
            for element in enabled:
                suffix = f".{int(kw['frame']):04d}" if self.frame_suffix else ""
                path = stem.parent / f"{stem.stem}.{element.elementname}{suffix}.exr"
                self.write_split(path, element, w, h)
        return bitmap

    def write_split(self, path: Path, element, w: int, h: int) -> None:
        layer = element.elementname
        if element.classname == "VRayCryptomatte":
            channels = {
                f"{layer}{level:02d}.{c}": np.zeros((h, w), np.float32) for level in range(3) for c in "RGBA"
            }
        elif element.classname == "VRayZDepth":
            channels = {"Z": np.full((h, w), 1234.5, np.float32)}
        else:
            channels = {c: np.zeros((h, w), np.float16) for c in "RGB"}
        header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
        path.parent.mkdir(parents=True, exist_ok=True)
        OpenEXR.File(header, channels).write(str(path))

    def vfb(self, *args):
        self.inner_vfb(*args)
        if len(args) >= 2 and str(args[0]).lower() == "saveimage" and str(args[1]).endswith(".png"):
            data = png16(*self.size) if self.png_bits == 16 else _png8(*self.size)
            Path(args[1]).write_bytes(data)


def _png8(width: int, height: int) -> bytes:
    from fake_pymxs import tiny_png

    return tiny_png(width, height)


def animate(scene, name: str, dolly_mm: float = 100.0, offset_mm: float = 0.0):
    """Make the camera's objecttransform follow the time slider: a dolly of dolly_mm per frame along
    its view direction (local -Z), starting offset_mm from where it stood."""
    cam = scene.cameras[name]
    rows = cam.__dict__["objecttransform"].rows
    back = rows[2]

    def at(frame) -> FakeMatrix3:
        f = float(getattr(frame, "frame", frame) or 0)
        d = dolly_mm * f + offset_mm
        p = rows[3]
        moved = (p.x - back.x * d, p.y - back.y * d, p.z - back.z * d)
        return FakeMatrix3((tuple(rows[0]), tuple(rows[1]), tuple(rows[2]), moved))

    cam.__class__ = type(
        "AnimatedCamera",
        (type(cam),),
        {"objecttransform": property(lambda self: at(scene.currentTime), lambda self, value: None)},
    )
    return cam


def motion_scene(install_scene, exclude: bool = True, override_names: str = "spec"):
    """The clubhouse at 160x90 with V-Ray simulated, glass that refracts (MAT_glass), one
    light-emitting material (SIGN_logo_main), the material override and split-output properties."""
    scene = install_scene("clubhouse")
    scene.renderWidth, scene.renderHeight = 160, 90
    scene.maxVersion = lambda: [28000, 63, 0, 28, 2, 0, 2201, 2026, ".2 Update"]
    gamma_colour(scene)
    for node in scene.objects:
        mat = getattr(node, "material", None)
        if getattr(mat, "name", None) == "MAT_glass":
            mat.refraction = FakeColor(255, 255, 255)
        if node.name == "SIGN_logo_main":
            node.material = FakeMaterial("MAT_led_logo", "VRayLightMtl")
    vray = FakeVRay(scene)
    vr = scene.renderers.current
    if override_names == "spec":
        vr.override_mtl_on, vr.override_mtl = False, None
        if exclude:
            vr.override_mtl_exclude = []
    else:
        vr.options_overrideMtl_on, vr.options_overrideMtl_mtl = False, None
    vr.output_splitfilename = ""
    vr.output_splitRGB = True
    vr.output_splitAlpha = True
    made = clay_material_factory(scene)
    return scene, vray, made


def send_hero(packets: Path, cfg: dict[str, Any], camera: str = "CAM_front"):
    [hero] = send_packet.run([camera], packets, cfg["daemon_url"], {"config": cfg, "project_id": "tula-wot"})
    assert hero.ok, (hero.stage, hero.errors)
    return hero


def motion_run(camera, hero_id, f0, f1, packets, cfg, crossings=(), **options):
    opts = {"config": cfg, "sequence_resolution": SEQ, **options}
    return send_motion.run(camera, hero_id, f0, f1, packets, cfg["daemon_url"], crossings, opts)


# --------------------------------------------------------------------------- pure parts


def test_sequence_size_is_720p_or_the_exact_aspect():
    assert motion.sequence_size(3840, 2160) == (1280, 720)
    assert motion.sequence_size(1920, 1080) == (1280, 720)
    w, h = motion.sequence_size(1920, 804)
    assert (w, h) == (1280, 536) and w * 804 == h * 1920
    w, h = motion.sequence_size(1920, 1280)
    assert w * 1280 == h * 1920 and abs(w - 1280) <= 3
    # a ratio too coarse for a smaller size keeps the render's own size, never more
    assert motion.sequence_size(3840, 1607) == (3840, 1607)
    assert motion.sequence_size(160, 90) == (160, 90)
    hero = {"resolution": {"width": 3840, "height": 2160}}
    assert send_motion.sequence_resolution(hero) == (1280, 720)
    assert send_motion.sequence_resolution(hero, (640, 360)) == (640, 360)
    with pytest.raises(ValueError, match="aspect"):
        send_motion.sequence_resolution(hero, (640, 400))


def test_check_frame0_accepts_1e6_and_refuses_more():
    hero = [[1.0, 0, 0, 10.0], [0, 1.0, 0, 20.0], [0, 0, 1.0, 30.0], [0, 0, 0, 1]]
    near = [list(r) for r in hero]
    near[0][3] += 5e-7
    assert motion.check_frame0([{"frame": 0, "world_from_camera": near}], hero) <= 1e-6
    far = [list(r) for r in hero]
    far[2][3] += 1e-3
    with pytest.raises(motion.MotionError) as err:
        motion.check_frame0([{"frame": 3, "world_from_camera": far}], hero)
    assert err.value.code == motion.FRAME0_MISMATCH and "frame 3" in err.value.message


def test_material_kind_finds_glass_and_lights_by_class(scene):
    glass = FakeMaterial("MAT_glass", "VRayMtl")
    glass.refraction = FakeColor(250, 250, 250)
    brick = FakeMaterial("MAT_brick", "VRayMtl")
    brick.refraction = FakeColor(0, 0, 0)
    mapped = FakeMaterial("MAT_frosted", "VRayMtl")
    mapped.texmap_refraction, mapped.texmap_refraction_on = object(), True
    led = FakeMaterial("MAT_led", "VRayLightMtl")
    lit = FakeMaterial("MAT_lit", "VRayMtl")
    lit.selfIllumination = FakeColor(255, 200, 100)
    multi = FakeMaterial("MAT_window_multi", "Multimaterial")
    multi.materialList = [brick, None, glass]
    assert motion.material_kind(glass) == "glass" and motion.material_kind(brick) is None
    assert motion.material_kind(mapped) == "glass"
    assert motion.material_kind(led) == "light" and motion.material_kind(lit) == "light"
    assert motion.material_kind(multi) == "glass" and motion.material_kind(None) is None


def test_png_info_reads_the_bit_depth(tmp_path):
    path = tmp_path / "c.png"
    path.write_bytes(png16(7, 5))
    assert motion.png_info(path) == (7, 5, 16)
    (tmp_path / "x.png").write_bytes(b"not a png at all, just bytes......")
    assert motion.png_info(tmp_path / "x.png") is None


def test_widen_factor_reapplies_only_an_overscan_widening():
    def hero(width, fx, overscan=True):
        cam = {"resolution": {"width": width, "height": 90}, "intrinsics_px": {"fx": fx}}
        if overscan:
            cam["overscan"] = {"engine_aspect": "16:9"}
        return cam

    fov = math.degrees(2 * math.atan(36.0 / (2 * 24.0)))
    fx = 24.0 * 160 / 36.0
    assert send_motion.widen_factor(hero(160, fx), {"fov_deg": {"horizontal": fov}}) == 1.0
    wide = send_motion.widen_factor(hero(160, 24.0 * 120 / 36.0), {"fov_deg": {"horizontal": fov}})
    assert wide == pytest.approx(160 / 120)
    with pytest.raises(ValueError, match="field of view"):
        send_motion.widen_factor(
            hero(160, 24.0 * 120 / 36.0, overscan=False), {"fov_deg": {"horizontal": fov}}
        )
    with pytest.raises(ValueError):  # a narrower view than the hero is a different lens
        send_motion.widen_factor(hero(160, fx * 1.2), {"fov_deg": {"horizontal": fov}})


def test_crossings_are_checked():
    good = send_motion.check_crossings([{"frame": 3, "kind": "threshold", "note": "gate line"}], 0, 4)
    assert good == [{"frame": 3, "kind": "threshold", "note": "gate line"}]
    for bad in (
        {"frame": 9, "kind": "threshold"},
        {"frame": 1, "kind": "door"},
        {"frame": 1.5, "kind": "occluder"},
    ):
        with pytest.raises(ValueError):
            send_motion.check_crossings([bad], 0, 4)


# --------------------------------------------------------------------------- camera frames on the fake


def test_camera_frames_follow_the_animated_camera(install_scene):
    scene, _, _ = motion_scene(install_scene)
    animate(scene, "CAM_front", dolly_mm=100.0)
    frames = motion.camera_frames("CAM_front", 0, 3)
    assert [f["frame"] for f in frames] == [0, 1, 2, 3]
    assert frames[0]["focal_length_mm"] == 24.0 and frames[0]["focus_distance"] > 0
    t = [np.array([r[3] for r in f["world_from_camera"][:3]]) for f in frames]
    assert np.linalg.norm(t[1] - t[0]) == pytest.approx(100.0)
    assert np.linalg.norm(t[3] - t[0]) == pytest.approx(300.0)
    forward = -np.array([r[2] for r in frames[0]["world_from_camera"][:3]])
    assert np.dot(t[1] - t[0], forward) == pytest.approx(100.0)  # a push-in along the view axis
    assert scene.currentTime.frame == 0  # the time slider is put back
    with pytest.raises(motion.MotionError, match="two frames"):
        motion.camera_frames("CAM_front", 2, 2)


# --------------------------------------------------------------------------- Send Motion end to end


def test_motion_packet_validates_with_daemon_hashes(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _, made = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front", dolly_mm=120.0)
    seq = SequenceVRay(scene)
    crossings = [{"frame": 3, "kind": "threshold", "note": "passes the gate line"}]
    progress: list[tuple[int, int, str]] = []
    result = motion_run(
        "CAM_front", hero.packet_id, 0, 4, packets, cfg, crossings, progress=lambda *a: progress.append(a)
    )
    assert result.ok, (result.stage, result.code, result.errors)
    root = Path(result.packet_dir)
    assert root.parent == packets and root.name == result.packet_id != hero.packet_id
    assert plate_spec.validate_packet_dir(root, check_files=True) == []

    manifest = read_json(root / "manifest.json")
    hero_manifest = read_json(Path(hero.packet_dir) / "manifest.json")
    assert manifest["kind"] == "motion" and manifest["tier"] == "A"
    assert manifest["motion"]["hero_still_packet_id"] == hero.packet_id
    assert manifest["motion"]["frame0_matches_hero"] is True
    assert manifest["motion"]["sequence_resolution"] == {"width": 64, "height": 36}
    assert manifest["motion"]["clay_override"] == {
        "material": "PLATE_clay_matte_0.7",
        "keeps_glass": True,
        "keeps_lights": True,
    }
    assert manifest["motion"]["path_crossings"] == crossings
    assert manifest["camera"]["frames"] == {"hero": 0, "start": 0, "end": 4, "fps": 25}
    assert manifest["camera"]["world_from_camera"] == hero_manifest["camera"]["world_from_camera"]
    assert manifest["files"]["beauty_exr"]["hash"] == hero_manifest["files"]["beauty_exr"]["hash"]
    assert (
        manifest["files"]["clay_seq"]["dtype"] == "uint16" and manifest["files"]["z_seq"]["dtype"] == "float"
    )
    assert manifest["policy"] == hero_manifest["policy"]
    assert not any("scene changed" in w for w in result.warnings), result.warnings

    frames = read_json(root / "camera_frames.json")
    assert frames["packet_id"] == result.packet_id and len(frames["frames"]) == 5
    assert frames["frames"][0]["world_from_camera"] == hero_manifest["camera"]["world_from_camera"]
    assert frames["frames"][4]["world_from_camera"] != frames["frames"][0]["world_from_camera"]
    legend = read_json(root / "id_legend.json")
    assert legend["packet_id"] == result.packet_id
    assert sorted(p.name for p in (root / "motion").iterdir())[:3] == [
        "clay.0000.png",
        "clay.0001.png",
        "clay.0002.png",
    ]
    assert len(list((root / "motion").glob("*.exr"))) == 10
    assert (root / "motion" / "z.blake3").read_text(encoding="utf-8").count("\n") == 5

    # the renders: 5 frames at the sequence size, frame numbers passed, only Z + object Cryptomatte
    assert [r["frame"] for r in seq.renders] == [0, 1, 2, 3, 4]
    assert all(r["size"] == SEQ and r["split"] and not r["raw"] for r in seq.renders)
    assert all(r["elements"] == ["CryptoObject", "VRayZDepth"] for r in seq.renders)
    # the daemon got the motion folder after the hero's
    assert daemon.posted[-1]["packet_path"] == str(root.resolve()) and len(daemon.posted) == 2
    assert result.frame0_deviation == 0.0 and result.code is None
    assert progress[-1][2].endswith("sent " + result.packet_id)

    # everything is put back: override off, split and raw output as the artist had them, all elements on
    vr = scene.renderers.current
    assert vr.override_mtl_on is False and vr.override_mtl is None and vr.override_mtl_exclude == []
    assert vr.output_splitgbuffer is True and vr.output_saveRawFile is False and vr.output_splitfilename == ""
    assert all(e.enabled for e in scene.element_manager.elements)
    assert (scene.renderWidth, scene.renderHeight) == (160, 90)
    assert not (root / ".plate-staging").exists() and not list(packets.glob("**/*.tmp"))
    assert len(made) == 1


def test_clay_override_is_recorded_with_glass_and_lights_kept(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, made = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene, frame_suffix=True)
    artist_list = [scene.getNodeByName("VEG_tree_01")]  # the artist's own override exclusions
    scene.renderers.current.override_mtl_exclude = artist_list
    result = motion_run("CAM_front", hero.packet_id, 0, 2, packets, cfg)
    assert result.ok, (result.code, result.errors)
    assert scene.renderers.current.override_mtl_exclude is artist_list  # put back after the sequence
    glass = sorted(n.name for n in scene.objects if getattr(n.material, "name", None) == "MAT_glass")
    assert len(glass) == 5
    for r in seq.renders:
        assert r["on"] is True and r["material"] == "PLATE_clay_matte_0.7"
        assert r["diffuse"] == FakeColor(178.5, 178.5, 178.5)  # 0.7 grey
        assert r["exclude"] == sorted(glass + ["SIGN_logo_main"])
    assert sorted(result.clay["kept_glass"]) == glass and result.clay["kept_lights"] == ["SIGN_logo_main"]
    assert (
        result.clay["switch"] == "override_mtl_on" and result.clay["exclude_prop"] == "override_mtl_exclude"
    )
    assert made[0].reflection == FakeColor(0, 0, 0)
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_glass_not_kept_when_the_renderer_has_no_exclude_list(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene, exclude=False, override_names="vray")
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene)
    result = motion_run("CAM_front", hero.packet_id, 0, 1, packets, cfg)
    assert result.ok, (result.code, result.errors)
    assert seq.renders[0]["on"] is True and seq.renders[0]["exclude"] == []
    block = read_json(Path(result.packet_dir) / "manifest.json")["motion"]["clay_override"]
    assert block == {"material": "PLATE_clay_matte_0.7", "keeps_glass": False, "keeps_lights": False}
    assert any("no exclude list" in w for w in result.warnings)
    assert scene.renderers.current.options_overrideMtl_on is False
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_frame0_mismatch_aborts_before_anything_renders(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _, made = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front", dolly_mm=100.0)
    seq = SequenceVRay(scene)
    result = motion_run("CAM_front", hero.packet_id, 2, 6, packets, cfg)  # the move starts elsewhere
    assert not result.ok and result.code == send_motion.FRAME0_MISMATCH and result.stage == "camera"
    assert "frame 2" in result.errors[0]
    assert seq.renders == [] and made == [] and result.packet_dir is None
    assert sorted(p.name for p in packets.iterdir()) == [hero.packet_id] and len(daemon.posted) == 1
    assert scene.renderers.current.override_mtl_on is False


def test_a_camera_moved_since_the_hero_is_a_frame0_mismatch(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front", dolly_mm=100.0, offset_mm=0.01)  # 10 microns at frame 0
    SequenceVRay(scene)
    result = motion_run("CAM_front", hero.packet_id, 0, 3, packets, cfg)
    assert result.code == send_motion.FRAME0_MISMATCH and not result.ok


def test_hero_problems_are_reported(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    missing = motion_run("CAM_front", "pkt_01JA7QZZ1K3M5P7R9T2V4X6Z8B", 0, 3, packets, cfg)
    assert missing.code == send_motion.HERO_PACKET and "send the still first" in missing.errors[0]
    hero = send_hero(packets, cfg, camera="CAM_entry")
    other = motion_run("CAM_front", hero.packet_id, 0, 3, packets, cfg)
    assert other.code == send_motion.HERO_MISMATCH and "CAM_entry" in other.errors[0]
    bad = motion_run("CAM_entry", hero.packet_id, 0, 3, packets, cfg, [{"frame": 1, "kind": "door"}])
    assert bad.code == send_motion.BAD_CROSSINGS
    short = motion_run("CAM_entry", hero.packet_id, 3, 3, packets, cfg)
    assert short.code == motion.CAMERA_FRAMES and not short.ok


def test_a_hero_given_by_folder_path_works(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    SequenceVRay(scene)
    result = motion_run("CAM_front", hero.packet_dir, 0, 1, packets, cfg, supersedes=None)
    assert result.ok, result.errors
    assert result.hero_packet_id == hero.packet_id


def test_no_split_output_means_no_sequence(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene)
    del scene.renderers.current.output_splitfilename
    result = motion_run("CAM_front", hero.packet_id, 0, 2, packets, cfg)
    assert result.code == motion.SEQUENCE_UNSUPPORTED and seq.renders == []
    assert scene.renderers.current.override_mtl_on is False  # the clay override is put back
    assert not (Path(result.packet_dir) / "manifest.json").exists()


def test_no_material_override_is_clay_unavailable(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene)
    del scene.renderers.current.override_mtl_on
    result = motion_run("CAM_front", hero.packet_id, 0, 2, packets, cfg)
    assert result.code == motion.CLAY_UNAVAILABLE and seq.renders == []


def test_an_8_bit_vfb_save_is_recorded_as_uint8(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    SequenceVRay(scene, png_bits=8)
    result = motion_run("CAM_front", hero.packet_id, 0, 1, packets, cfg)
    assert result.ok, result.errors
    assert read_json(Path(result.packet_dir) / "manifest.json")["files"]["clay_seq"]["dtype"] == "uint8"
    assert any("8-bit" in w for w in result.warnings)


def test_a_render_failure_mid_sequence_leaves_nothing_ingestible(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene)
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("V-Ray: out of memory")
        return seq.render(*args, **kw)

    scene.render = flaky
    result = motion_run("CAM_front", hero.packet_id, 0, 4, packets, cfg)
    assert result.code == motion.SEQUENCE_FAILED and "out of memory" in result.errors[0]
    assert not (Path(result.packet_dir) / "manifest.json").exists()
    assert any("no manifest" in w for w in result.warnings) and len(daemon.posted) == 1
    vr = scene.renderers.current
    assert vr.override_mtl_on is False and vr.output_splitgbuffer is True
    assert all(e.enabled for e in scene.element_manager.elements)


def test_an_incompatible_daemon_stops_before_rendering(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _, _ = motion_scene(install_scene)
    hero = send_hero(packets, cfg)
    animate(scene, "CAM_front")
    seq = SequenceVRay(scene)
    daemon.health = {**daemon.health, "packet_versions": ["0.2"]}
    result = motion_run("CAM_front", hero.packet_id, 0, 2, packets, cfg)
    assert result.code == send_motion.DAEMON and result.stage == "daemon" and seq.renders == []
