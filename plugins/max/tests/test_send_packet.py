"""actions.send_packet end to end on the fake scene, plus the stdlib EXR header reader it relies on.

V-Ray is simulated here (FakeVRay): with the renderer's raw-image output on, render() writes a real
multilayer EXR (OpenEXR from the dev venv) holding every render element in the element manager, with
Cryptomatte ids, coverage and manifests built from the scene's pixel shares; the VFB save writes a
16-bit TIFF of the rendered size. The daemon is a stdlib http.server (health, session, /hash with
BLAKE3 from the dev venv, /packets, the packet event log). Every packet written is checked with
plate_spec.validate_packet_dir.
"""

from __future__ import annotations

import json
import math
import struct
import threading
import time
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from packet_fixture import STUB_TOKEN, StubDaemon

import plate_spec
from plate_max import PLUGIN_VERSION, exr_header, legend, policy, transport
from plate_max.actions import send_packet
from plate_max.bridge import hashing
from plate_max.recipes import CameraRecipe

np = pytest.importorskip("numpy")
OpenEXR = pytest.importorskip("OpenEXR")

REPO = Path(__file__).resolve().parents[3]
ROOM_A = REPO / "fixtures" / "packets" / "room_a"
COMPRESSIONS = {
    "none": OpenEXR.NO_COMPRESSION,
    "zips": OpenEXR.ZIPS_COMPRESSION,
    "zip": OpenEXR.ZIP_COMPRESSION,
    "piz": OpenEXR.PIZ_COMPRESSION,
}


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    """The Send journal goes under PLATE_HOME: keep it inside the test's folder."""
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


# --------------------------------------------------------------------------- stub daemon


class Daemon:
    """GET /api/v0/health, GET /api/v0/session, POST /api/v0/hash (packet_fixture.StubDaemon), POST
    /api/v0/packets and GET /api/v0/packets/<id>/events.

    post_script, when set, answers the POSTs in turn: (status, body, delay_s) each, the last one
    repeating; a delay sleeps before answering (a daemon slower than the plugin's timeout). events is
    the event log every packet's events route serves (default: one ingest.done)."""

    def __init__(self, root: Path):
        self.hasher = StubDaemon(root)
        self.health: dict[str, Any] = {
            "ok": True,
            "daemon_version": PLUGIN_VERSION,
            "packet_versions": ["0.1"],
        }
        self.packets_dirs = [str(root)]
        self.session_status: int | None = None  # answer /session with this status instead
        self.post_status = 202
        self.post_body: dict[str, Any] = {"status": "ingesting"}
        self.post_script: list[tuple[int, dict[str, Any], float]] = []
        self.posted: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = [{"seq": 1, "type": "ingest.done", "data": {}}]
        self.event_requests: list[str] = []
        self.health_calls = 0
        self.session_calls = 0

    def next_post(self) -> tuple[int, dict[str, Any], float]:
        if not self.post_script:
            return self.post_status, self.post_body, 0.0
        return self.post_script.pop(0) if len(self.post_script) > 1 else self.post_script[0]

    def sse(self, after: int) -> bytes:
        frames = ["retry: 1000\n\n"]
        for ev in self.events:
            if int(ev.get("seq", 0)) > after:
                frames.append(f"id: {ev.get('seq')}\ndata: {json.dumps(ev)}\n\n")
        return "".join(frames).encode("utf-8")


@contextmanager
def serve_daemon(daemon: Daemon):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body):
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {daemon.hasher.token}"

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/v0/health":
                daemon.health_calls += 1
                self._send(200, daemon.health)
            elif url.path == "/api/v0/session":
                daemon.session_calls += 1
                if not self._authorised():
                    return self._send(401, {"error": "unauthorized"})
                if daemon.session_status is not None:
                    return self._send(daemon.session_status, {"error": "session refused"})
                self._send(200, {**daemon.health, "packets_dirs": list(daemon.packets_dirs)})
            elif url.path.startswith("/api/v0/packets/") and url.path.endswith("/events"):
                daemon.event_requests.append(self.path)
                if not self._authorised():
                    return self._send(401, {"error": "unauthorized"})
                after = int(dict(urllib.parse.parse_qsl(url.query)).get("after", "0"))
                raw = daemon.sse(after)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._send(404, {"error": "unknown route"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/api/v0/hash":
                self._send(*daemon.hasher.answer(self.headers, body))
            elif self.path == "/api/v0/packets":
                if not self._authorised():
                    return self._send(401, {"error": "unauthorized"})
                daemon.posted.append({**body, "version": self.headers.get("X-Plate-Packet-Version")})
                status, answer, delay = daemon.next_post()
                if delay:
                    time.sleep(delay)
                try:
                    self._send(status, answer)
                except OSError:  # the plugin gave up waiting and closed the connection
                    pass
            else:
                self._send(404, {"error": "unknown route"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"token": STUB_TOKEN, "daemon_url": f"http://127.0.0.1:{server.server_address[1]}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- simulated V-Ray


def tiff16(width: int, height: int, samples: int = 3) -> bytes:
    """A little-endian uncompressed 16-bit TIFF of width x height (zeros), stdlib struct only."""
    tags = [
        (256, 4, 1, width),
        (257, 4, 1, height),
        (258, 3, samples, None),  # bits per sample: an offset to `samples` SHORTs
        (259, 3, 1, 1),
        (262, 3, 1, 2),
        (273, 4, 1, None),  # strip offset
        (277, 3, 1, samples),
        (278, 4, 1, height),
        (279, 4, 1, width * height * samples * 2),
    ]
    ifd_size = 2 + 12 * len(tags) + 4
    bits_at = 8 + ifd_size
    pixels_at = bits_at + 2 * samples
    out = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(tags))
    for tag, kind, count, value in tags:
        if tag == 258:
            value = bits_at
        elif tag == 273:
            value = pixels_at
        if kind == 3 and count == 1:
            out += struct.pack("<HHIHH", tag, kind, count, value, 0)
        else:
            out += struct.pack("<HHII", tag, kind, count, value)
    out += struct.pack("<I", 0) + struct.pack(f"<{samples}H", *([16] * samples))
    return out + b"\x00" * (width * height * samples * 2)


def crypto_key(layer: str) -> str:
    return f"{legend.murmur3_32(layer.encode('utf-8')):08x}"[:7]


def hex_id(name: str) -> str:
    return plate_spec.cryptomatte_id(name)[2:]


class FakeVRay:
    """Makes the fake renderer behave like V-Ray for render_packet_frame: raw EXR + 16-bit VFB TIFF.

    shares {node name: share} (default: the scene's own pixel shares) decide the rank-0 Cryptomatte
    ids; extra_names enter the object manifest without coverage; tamper(header, channels) may edit
    the file before it is written."""

    def __init__(self, scene, shares=None, compression="zip", extra_names=(), drop=(), tamper=None):
        self.scene = scene
        self.shares = dict(scene.pixel_shares if shares is None else shares)
        self.compression = compression
        self.extra_names = list(extra_names)
        self.drop = set(drop)
        self.tamper = tamper
        self.written: list[Path] = []
        self.counts: dict[str, int] = {}  # rank-0 pixels per object name in the last EXR
        self.last_size = (scene.renderWidth, scene.renderHeight)
        self.film_during_render: list[float] = []
        vr = scene.renderers.current
        vr.output_saveRawFile = False
        vr.output_rawFileName = ""
        vr.output_genpreview = True
        vr.output_on = True
        vr.output_splitgbuffer = True
        vr.output_rawExrBitsPerChannel = 16
        vr.output_rawExrCompression = 4
        self.vr = vr
        self.plain_render, self.plain_vfb = scene.render, scene.vfbControl
        scene.render, scene.vfbControl = self.render, self.vfb

    def render(self, *args, **kw):
        bitmap = self.plain_render(*args, **kw)
        width, height = int(kw["outputwidth"]), int(kw["outputheight"])
        self.last_size = (width, height)
        cam = kw.get("camera")
        self.film_during_render.append(getattr(cam, "film_width", None))
        if self.vr.output_saveRawFile and self.vr.output_rawFileName:
            path = Path(self.vr.output_rawFileName)
            self.write_exr(path, width, height)
            self.written.append(path)
        return bitmap

    def vfb(self, *args):
        self.plain_vfb(*args)
        if len(args) >= 2 and str(args[0]).lower() == "saveimage":
            Path(args[1]).write_bytes(tiff16(*self.last_size))

    def _materials(self) -> dict[str, str | None]:
        out = {}
        for node in self.scene.objects:
            mat = getattr(node, "material", None)
            out[node.name] = getattr(mat, "name", None)
        return out

    def write_exr(self, path: Path, width: int, height: int) -> None:
        n = width * height
        obj_ids = np.zeros(n, np.uint32)
        mat_ids = np.zeros(n, np.uint32)
        cov = np.zeros(n, np.float32)
        materials = self._materials()
        cursor = 0
        for name, share in self.shares.items():
            count = min(int(round(share * n)), n - cursor)
            self.counts[name] = count
            obj_ids[cursor : cursor + count] = int(hex_id(name), 16)
            mat = materials.get(name)
            if mat:
                mat_ids[cursor : cursor + count] = int(hex_id(mat), 16)
            cov[cursor : cursor + count] = 1.0
            cursor += count
        channels: dict[str, Any] = {}
        zeros_h = np.zeros((height, width), np.float16)
        for c in "RGBA":
            channels[c] = zeros_h + np.float16(0.5)
        header: dict[str, Any] = {
            "compression": COMPRESSIONS[self.compression],
            "type": OpenEXR.scanlineimage,
        }
        for element in self.scene.element_manager.elements:
            if not element.enabled:
                continue
            layer = element.elementname
            if layer in self.drop:
                continue
            cls = element.classname
            if cls == "VRayZDepth":
                channels[f"{layer}.Z"] = np.full((height, width), 7.5, np.float32)
            elif cls == "VRayNormals":
                for c in "XYZ":
                    channels[f"{layer}.{c}"] = zeros_h
            elif cls == "VRayCryptomatte":
                ids = mat_ids if element.idType == 2 else obj_ids
                for level in range(3):
                    for c in "RGBA":
                        channels[f"{layer}{level:02d}.{c}"] = np.zeros((height, width), np.float32)
                channels[f"{layer}00.R"] = ids.view(np.float32).reshape(height, width).copy()
                channels[f"{layer}00.G"] = cov.reshape(height, width).copy()
                key = crypto_key(layer)
                if element.idType == 2:
                    names = sorted({m for m in materials.values() if m})
                else:
                    names = list(self.shares) + self.extra_names
                header[f"cryptomatte/{key}/name"] = layer
                header[f"cryptomatte/{key}/hash"] = "MurmurHash3_32"
                header[f"cryptomatte/{key}/conversion"] = "uint32_to_float32"
                header[f"cryptomatte/{key}/manifest"] = json.dumps({nm: hex_id(nm) for nm in names})
            else:
                for c in "RGB":
                    channels[f"{layer}.{c}"] = zeros_h
        if self.tamper:
            self.tamper(header, channels)
        path.parent.mkdir(parents=True, exist_ok=True)
        OpenEXR.File(header, channels).write(str(path))


def gamma_colour(scene, gamma: float = 2.2) -> None:
    """Max's colour pipeline in the gamma workflow (the fake has no ColorPipelineMgr by default)."""
    scene.ColorPipelineMgr = SimpleNamespace(Mode="Gamma", outputGamma=gamma, inputGamma=gamma)


def vray_scene(install_scene, name="clubhouse", size=(160, 90), colour=True, **vray_kw):
    scene = install_scene(name)
    scene.renderWidth, scene.renderHeight = size
    scene.maxVersion = lambda: [28000, 63, 0, 28, 2, 0, 2201, 2026, ".2 Update"]
    if colour:
        gamma_colour(scene)
    return scene, FakeVRay(scene, **vray_kw)


@pytest.fixture
def daemon_env(tmp_path):
    """(daemon, config, packets folder) with the stub daemon serving."""
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def send(cameras, packets, cfg, **options):
    opts = {"config": cfg, "project_id": "tula-wot", "staging_dir": None, **options}
    return send_packet.run(cameras, packets, cfg["daemon_url"], opts)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- overscan math


def test_overscan_grows_the_short_dimension_and_centres_the_frame():
    assert send_packet.overscan_size(1920, 804, "16:9") == (1920, 1080, 0, 138)
    plan = send_packet.plan_frame(1920, 804, CameraRecipe(camera="C", overscan_aspect="16:9"))
    assert (plan.width, plan.height) == (1920, 1080)
    assert plan.safe_frame == {"x": 0, "y": 138, "width": 1920, "height": 804}
    assert plan.overscan["engine_aspect"] == "16:9" and plan.widen == 1.0


def test_overscan_that_adds_width_widens_the_view():
    assert send_packet.overscan_size(1920, 1280, "16:9") == (2276, 1280, 178, 0)
    plan = send_packet.plan_frame(1920, 1280, CameraRecipe(camera="C", overscan_aspect="16:9"))
    assert plan.safe_frame == {"x": 178, "y": 0, "width": 1920, "height": 1280}
    assert plan.widen == pytest.approx(2276 / 1920)


def test_a_frame_at_the_engine_aspect_is_unchanged():
    assert send_packet.overscan_size(3840, 2160, "16:9") == (3840, 2160, 0, 0)
    plan = send_packet.plan_frame(3840, 2160, CameraRecipe(camera="C"))
    assert plan.overscan is None and plan.safe_frame == {"x": 0, "y": 0, "width": 3840, "height": 2160}


def test_a_recipe_safe_frame_moves_with_the_overscan_and_needs_an_overscan_block():
    rec = CameraRecipe(camera="C", safe_frame={"x": 0, "y": 10, "w": 1920, "h": 700}, overscan_aspect="16:9")
    plan = send_packet.plan_frame(1920, 804, rec)
    assert plan.safe_frame == {"x": 0, "y": 148, "width": 1920, "height": 700}
    bare = send_packet.plan_frame(1920, 1080, CameraRecipe(camera="C", safe_frame=(0, 138, 1920, 804)))
    assert bare.overscan["engine_aspect"] == "16:9" and (bare.width, bare.height) == (1920, 1080)
    with pytest.raises(ValueError, match="outside"):
        send_packet.plan_frame(100, 100, CameraRecipe(camera="C", safe_frame=(50, 50, 60, 10)))


# --------------------------------------------------------------------------- the full Send


def test_full_send_writes_a_packet_plate_spec_accepts(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene, extra_names=["LGT_helper"])
    progress: list[tuple[int, int, str]] = []
    [result] = send(["CAM_front"], packets, cfg, progress=lambda *a: progress.append(a))
    assert result.ok, (result.stage, result.errors)
    assert result.stage == "done" and result.status_code == 202
    root = Path(result.packet_dir)
    assert root.parent == packets and root.name == result.packet_id
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    assert sorted(p.name for p in root.iterdir()) == [
        "beauty.exr",
        "display.tif",
        "id_legend.json",
        "manifest.json",
        "scene_digest.txt",
    ]

    manifest, leg = read_json(root / "manifest.json"), read_json(root / "id_legend.json")
    # the scene digest is a file of the packet, and the scene hash is its BLAKE3
    digest_ref = manifest["files"]["x_scene_digest"]
    assert digest_ref["path"] == "scene_digest.txt" and digest_ref["kind"] == "file"
    assert digest_ref["hash"]["hex"] == manifest["scene_hash"]["hex"]
    assert (root / "scene_digest.txt").read_bytes().startswith(b"plate-scene-digest 1\n")
    assert manifest["packet_id"] == result.packet_id and manifest["project_id"] == "tula-wot"
    assert manifest["shot_id"] == "cam-front" and manifest["host"]["version"] == "2026.2"
    assert manifest["renderer"] == {"name": "vray", "version": "V_Ray_7_hotfix_3"}
    assert (
        manifest["colour"]["max_colour_mode"] == "legacy_gamma" and manifest["colour"]["legacy_gamma"] == 2.2
    )
    assert (
        manifest["camera"]["resolution"]["width"] == 160 and manifest["camera"]["resolution"]["height"] == 90
    )
    exr = manifest["files"]["beauty_exr"]
    assert exr["compression"] == "zip" and (exr["width"], exr["height"]) == (160, 90)
    assert [e["layer"] for e in exr["elements"]] == [
        "RGBA",
        "VRayZDepth",
        "VRayNormals",
        "CryptoObject",
        "CryptoMaterial",
        "VRayWireColor",
        "VRayDiffuseFilter",
    ]
    assert manifest["render"]["session"] == "interactive" and manifest["render"]["peak_rss_gb"] == 12.0

    # legend = every covered name of the Cryptomatte manifest, shares measured from the EXR
    objects = {e["name"]: e for e in leg["entries"] if e["kind"] == "object"}
    assert len(objects) == 40 and "LGT_helper" not in objects
    for name, share in scene.pixel_shares.items():
        assert objects[name]["pixel_share"] == pytest.approx(vray.counts[name] / (160 * 90), abs=1e-6)
        assert objects[name]["pixel_share"] == pytest.approx(share, abs=2e-4)
    assert objects["Box042"]["class_source"] == "unlabelled"
    coverage = manifest["id_legend"]["coverage"]
    assert coverage["unlabelled_share"] == pytest.approx(0.04, abs=2e-3)
    editable = set(manifest["policy"]["editable_ids"])
    assert objects["VEG_tree_01"]["id"] in editable and objects["SKY_dome"]["id"] in editable
    assert objects["ARCH_wall_north"]["id"] not in editable and objects["Box042"]["id"] not in editable

    # the render: VFB on, at the planned size, V-Ray settings and scene size put back
    call = scene.render_calls[-1]
    assert call["vfb"] is True and (call["outputwidth"], call["outputheight"]) == (160, 90)
    assert vray.vr.output_saveRawFile is False and (scene.renderWidth, scene.renderHeight) == (160, 90)
    # the daemon got the folder, with the packet version header
    assert daemon.posted[-1]["packet_path"] == str(root.resolve())
    assert daemon.posted[-1]["version"] == "0.1.1" and daemon.health_calls == 1
    assert daemon.session_calls == 1  # the preflight's token and folder check
    # after the 202 the packet's event log was followed until ingest.done
    assert result.ingest == "ingested" and result.code is None
    assert result.events_url == f"/api/v0/packets/{result.packet_id}/events"
    assert "wait_s=0" in daemon.event_requests[0]
    out = result.as_dict()
    assert {k: out[k] for k in ("status_code", "packet_id", "ingest")} == {
        "status_code": 202,
        "packet_id": result.packet_id,
        "ingest": "ingested",
    }
    assert progress[0][0] == 0 and progress[-1][:2] == (1, 1) and "sent" in progress[-1][2]
    journal = read_json(Path(result.journal))
    assert journal["finished"] == "ok" and journal["steps"][-1]["step"] == "post"
    assert not list(packets.glob("**/*.tmp")) and result.as_dict()["ok"] is True


def test_send_with_overscan_records_the_safe_frame(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene, size=(192, 80))
    recipes = {"CAM_front": CameraRecipe(camera="CAM_front", overscan_aspect="16:9")}
    [result] = send(["CAM_front"], packets, cfg, recipes=recipes)
    assert result.ok, result.errors
    manifest = read_json(Path(result.packet_dir) / "manifest.json")
    cam = manifest["camera"]
    assert cam["resolution"]["width"] == 192 and cam["resolution"]["height"] == 108
    assert cam["safe_frame"] == {"x": 0, "y": 14, "width": 192, "height": 80}
    assert cam["overscan"]["engine_aspect"] == "16:9"
    assert scene.render_calls[-1]["outputheight"] == 108
    assert (scene.renderWidth, scene.renderHeight) == (192, 80)  # the artist's size is back
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_overscan_that_adds_width_keeps_the_pixel_scale(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene, size=(120, 90))
    cam_node = scene.cameras["CAM_front"]
    film = cam_node.film_width
    recipes = {"CAM_front": {"overscan_aspect": "16:9"}}
    [result] = send(["CAM_front"], packets, cfg, recipes=recipes)
    assert result.ok, result.errors
    cam = read_json(Path(result.packet_dir) / "manifest.json")["camera"]
    assert cam["resolution"]["width"] == 160 and cam["safe_frame"] == {
        "x": 20,
        "y": 0,
        "width": 120,
        "height": 90,
    }
    fx_base = cam_node.focal_length * 120 / film
    assert cam["intrinsics_px"]["fx"] == pytest.approx(fx_base, rel=1e-9)
    assert cam["film_width_mm"] == pytest.approx(film * 160 / 120)
    assert vray.film_during_render == [pytest.approx(film * 160 / 120)]
    assert cam_node.film_width == film  # put back after the render
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_unknown_colour_mode_aborts_before_render(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene, colour=False)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "colour"
    assert "colour" in result.errors[0] and "ColorPipelineMgr" in result.errors[0]
    assert scene.render_calls == [] and list(packets.iterdir()) == []


def test_lint_failure_aborts_before_render(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene, name="shifted_camera")
    [result] = send(["CAM_shifted"], packets, cfg)
    assert not result.ok and result.stage == "lint"
    assert policy.SHIFTED_CAMERA in [p["code"] for p in result.problems]
    assert result.errors[0].startswith("lint:")
    assert scene.render_calls == [] and scene.element_manager.elements == []


def test_daemon_426_is_a_failed_send_naming_the_version(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    daemon.post_status, daemon.post_body = 426, {"error": "packet version 0.1.1 unsupported"}
    vray_scene(install_scene)
    [result] = send(["CAM_front"], packets, cfg)
    assert result.ok is False and result.status_code == 426 and result.stage == "post"
    assert "version" in result.errors[0]
    # the folder is complete and valid: it can be posted again once the daemon is updated
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_an_incompatible_daemon_stops_every_camera_before_rendering(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    daemon.health = {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.2"]}
    scene, _ = vray_scene(install_scene)
    results = send(["CAM_front", "CAM_aerial"], packets, cfg)
    assert [r.ok for r in results] == [False, False]
    assert all("version" in r.errors[0] and r.stage == "preflight" for r in results)
    assert {r.code for r in results} == {send_packet.PREFLIGHT_VERSION}
    assert scene.render_calls == [] and daemon.health_calls == 1 and daemon.session_calls == 0


def test_an_unreachable_daemon_is_reported_not_raised(install_scene, tmp_path):
    vray_scene(install_scene)
    cfg = {"token": STUB_TOKEN, "daemon_url": "http://127.0.0.1:9"}
    [result] = send_packet.run(["CAM_front"], tmp_path, cfg["daemon_url"], {"config": cfg})
    assert not result.ok and "unreachable" in result.errors[0]
    assert result.stage == "preflight" and result.code == send_packet.PREFLIGHT_UNREACHABLE


def test_one_bad_camera_never_stops_the_others(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    results = send(["CAM_front", "CAM_missing", "CAM_aerial"], packets, cfg)
    assert [r.camera for r in results] == ["CAM_front", "CAM_missing", "CAM_aerial"]
    assert [r.ok for r in results] == [True, False, True]
    assert "CAM_missing" in results[1].errors[0]
    assert len({r.packet_id for r in results if r.ok}) == 2


def test_a_render_failure_is_contained(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("V-Ray: out of memory")
        return vray.render(*args, **kw)

    scene.render = flaky
    results = send(["CAM_front", "CAM_entry"], packets, cfg)
    assert results[0].stage == "render" and "out of memory" in results[0].errors[0]
    assert results[1].ok, results[1].errors
    assert scene.renderers.current.output_saveRawFile is False


def test_backup_runs_before_the_scene_is_changed(install_scene, daemon_env, tmp_path):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    scene.maxFilePath, scene.maxFileName = str(tmp_path), "clubhouse.max"
    (tmp_path / "clubhouse.max").write_bytes(b"MAX")
    [result] = send(["CAM_front"], packets, cfg, backup=True)
    assert result.ok, result.errors
    assert Path(result.backup).is_file() and ".plate-backup." in result.backup
    assert scene.save_calls[0]["useNewFile"] is False
    assert read_json(Path(result.journal))["backup"] == result.backup
    assert read_json(Path(result.packet_dir) / "manifest.json")["project_id"] == "tula-wot"


def test_the_project_id_defaults_to_the_scene_name(install_scene, daemon_env, tmp_path):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    scene.maxFilePath, scene.maxFileName = str(tmp_path), "Clubhouse Final v3.max"
    [result] = send_packet.run(["CAM_front"], packets, cfg["daemon_url"], {"config": cfg})
    assert result.ok, result.errors
    assert read_json(Path(result.packet_dir) / "manifest.json")["project_id"] == "clubhouse-final-v3"


def test_unlabelled_share_over_the_limit_blocks_the_packet(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene = install_scene("clubhouse")
    shares = dict(scene.pixel_shares)
    shares.update({"Box042": 0.035, "Teapot01": 0.035, "Sphere007": 0.01, "SKY_dome": 0.08})
    scene.renderWidth, scene.renderHeight = 160, 90
    gamma_colour(scene)
    FakeVRay(scene, shares=shares)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "legend"
    assert [p["code"] for p in result.problems] == [policy.UNLABELLED_SHARE]
    assert not (Path(result.packet_dir) / "manifest.json").exists()  # never ingestible
    assert any("no manifest" in w for w in result.warnings)


def test_a_packets_folder_outside_the_session_is_refused_before_rendering(
    install_scene, daemon_env, tmp_path
):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    elsewhere = tmp_path / "not-the-packets-folder"
    [result] = send(["CAM_front"], elsewhere, cfg)
    assert not result.ok and result.stage == "preflight" and result.code == send_packet.PREFLIGHT_OUT_ROOT
    assert result.errors[0].startswith(f"{elsewhere} is outside the daemon's packets folders: ")
    assert str(packets) in result.errors[0]
    assert scene.render_calls == [] and not elsewhere.exists()


def test_a_hash_refusal_after_the_preflight_is_still_reported(install_scene, daemon_env, tmp_path):
    """The session lists a folder /hash then refuses (a daemon reconfigured mid-Send): stage hash, 403."""
    daemon, cfg, _ = daemon_env
    vray_scene(install_scene)
    elsewhere = tmp_path / "not-the-packets-folder"
    daemon.packets_dirs.append(str(elsewhere))
    [result] = send(["CAM_front"], elsewhere, cfg)
    assert not result.ok and result.stage == "hash"
    assert "daemon" in result.errors[0] and "403" in result.errors[0]


def test_recipe_classes_only_narrow_what_is_editable(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    recipes = {"CAM_front": CameraRecipe(camera="CAM_front", editable_classes=["vegetation"])}
    [result] = send(["CAM_front"], packets, cfg, recipes=recipes)
    assert result.ok, result.errors
    root = Path(result.packet_dir)
    manifest, leg = read_json(root / "manifest.json"), read_json(root / "id_legend.json")
    by_id = {e["id"]: e for e in leg["entries"]}
    editable = [by_id[i] for i in manifest["policy"]["editable_ids"]]
    assert editable and {e["class"] for e in editable} == {"vegetation"}
    assert plate_spec.validate_packet_dir(root, check_files=True) == []


def test_a_never_editable_class_in_a_recipe_is_refused(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    recipes = {"CAM_front": {"editable_classes": ["glazing"]}}
    [result] = send(["CAM_front"], packets, cfg, recipes=recipes)
    assert not result.ok and result.stage == "recipe" and "never" in result.errors[0]
    assert scene.render_calls == []


def test_a_covered_name_that_is_not_a_scene_node_enters_locked(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    scene = install_scene("clubhouse")
    shares = dict(scene.pixel_shares)
    shares["Box042"] = 0.0  # keep the unlabelled total under the 5% lint with the proxy's 2%
    shares["SKY_dome"] = 0.115
    shares["ForestPack_item_7"] = 0.02
    scene.renderWidth, scene.renderHeight = 160, 90
    gamma_colour(scene)
    FakeVRay(scene, shares=shares)
    [result] = send(["CAM_front"], packets, cfg)
    assert result.ok, result.errors
    leg = read_json(Path(result.packet_dir) / "id_legend.json")
    entry = next(e for e in leg["entries"] if e["name"] == "ForestPack_item_7")
    assert entry["class_source"] == "unlabelled" and entry["pixel_share"] == pytest.approx(0.02, abs=1e-3)
    assert "Box042" not in {e["name"] for e in leg["entries"]}  # no coverage, no entry
    assert any("ForestPack_item_7" in w for w in result.warnings)


def test_a_lossy_exr_is_refused(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene, compression="piz")
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "exr" and "piz" in result.errors[0]
    assert list(packets.iterdir()) == []


def test_an_element_missing_from_the_exr_is_refused(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene, drop={"VRayWireColor"})
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "exr" and "VRayWireColor" in result.errors[0]


def test_a_half_float_depth_pass_is_refused(install_scene, daemon_env):
    _, cfg, packets = daemon_env

    def halve(header, channels):
        channels["VRayZDepth.Z"] = channels["VRayZDepth.Z"].astype(np.float16)

    vray_scene(install_scene, tamper=halve)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "exr" and "32-bit float" in result.errors[0]


def test_a_manifest_id_that_is_not_the_hash_of_its_name_stops_the_send(install_scene, daemon_env):
    _, cfg, packets = daemon_env

    def swap(header, channels):
        key = crypto_key("CryptoObject")
        names = json.loads(header[f"cryptomatte/{key}/manifest"])
        names["SKY_dome"], names["PEOPLE_01"] = names["PEOPLE_01"], names["SKY_dome"]
        header[f"cryptomatte/{key}/manifest"] = json.dumps(names)

    vray_scene(install_scene, tamper=swap)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "legend" and "MurmurHash3" in result.errors[0]


def test_opt_in_elements_reach_the_manifest(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    recipes = {
        "CAM_front": CameraRecipe(camera="CAM_front", extra_elements=["VRayRawLighting", "VRayLightSelect"])
    }
    [result] = send(["CAM_front"], packets, cfg, recipes=recipes)
    assert result.ok, result.errors
    els = read_json(Path(result.packet_dir) / "manifest.json")["files"]["beauty_exr"]["elements"]
    by_layer = {e["layer"]: e for e in els}
    assert by_layer["VRayRawLighting"]["element"] == "raw_lighting"
    assert (
        by_layer["LS_Sun01"]["light_name"] == "Sun01" and by_layer["LS_Dome01"]["element"] == "light_select"
    )


def test_supersedes_is_written_per_camera(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    [first] = send(["CAM_front"], packets, cfg)
    [second] = send(["CAM_front"], packets, cfg, supersedes={"CAM_front": first.packet_id})
    assert second.ok, second.errors
    assert read_json(Path(second.packet_dir) / "manifest.json")["supersedes"] == first.packet_id


def test_dock_options_object_and_recipe_subjects_are_accepted(install_scene, daemon_env):
    """The dock passes an attribute-readable dict; batch callers may pass recipes instead of names."""
    from plate_max.ui.controller import ActionOptions

    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    opts = ActionOptions(recipes={}, progress=None, backup=False, config=cfg, project_id="tula-wot")
    results = send_packet.run([CameraRecipe(camera="CAM_entry")], packets, cfg["daemon_url"], opts)
    assert [r.camera for r in results] == ["CAM_entry"] and results[0].ok, results[0].errors


# --------------------------------------------------------------------------- big payloads, planar, post


def test_a_30000_line_scene_digest_is_hashed_by_path_and_validates(install_scene, daemon_env):
    """30 000 extra geometry nodes: the digest text is far over the payload limit (and the daemon's
    4 MiB cap), so it must travel as scene_digest.txt, never as a payload after the render."""
    from fake_pymxs import make_node

    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    scene.objects.extend(
        make_node(f"ARCH_filler_{i:05d}", "ARCH_filler", "MAT_brick", x=i) for i in range(30000)
    )
    [result] = send(["CAM_front"], packets, cfg)
    assert result.ok, (result.stage, result.errors)
    root = Path(result.packet_dir)
    text = (root / "scene_digest.txt").read_bytes()
    assert text.count(b"\nnode|") >= 30000 and len(text) > hashing.PAYLOAD_LIMIT
    payload_sizes = [
        len(v) * 3 // 4 for body in daemon.hasher.requests for v in (body.get("payloads") or {}).values()
    ]
    assert max(payload_sizes) <= hashing.PAYLOAD_LIMIT  # nothing big went in memory
    assert any(str(root / "scene_digest.txt") in (body.get("paths") or []) for body in daemon.hasher.requests)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []


def test_planar_follows_the_class_rule_and_the_user_property(install_scene, daemon_env):
    from fake_pymxs import make_node

    _, cfg, packets = daemon_env
    scene = install_scene("clubhouse")
    scene.objects.append(make_node("GRND_terrace", "GRND_terrace", "MAT_stone"))
    shares = dict(scene.pixel_shares)
    shares["SKY_dome"] = 0.08
    shares["GRND_terrace"] = 0.03
    shares["ARCH_column_02"] = 0.004  # under the 0.5 % floor: a sliver is not a planar region
    scene.renderWidth, scene.renderHeight = 160, 90
    gamma_colour(scene)
    FakeVRay(scene, shares=shares)
    wall = scene.getNodeByName("ARCH_wall_north")
    wall.user_props["plate_planar"] = "false"  # the artist knows this wall is a fluted relief
    [result] = send(["CAM_front"], packets, cfg)
    assert result.ok, result.errors
    leg = read_json(Path(result.packet_dir) / "id_legend.json")
    objects = {e["name"]: e for e in leg["entries"] if e["kind"] == "object"}
    planar = {name for name, e in objects.items() if e.get("planar") is True}
    assert {"ARCH_wall_south", "GLZ_curtainwall_s", "HARD_paving_01", "GRND_terrace"} <= planar
    assert objects["GRND_terrace"]["class"] == "ground"
    assert objects["ARCH_wall_north"]["planar"] is False  # the property wins over the rule
    assert "planar" not in objects["ARCH_column_02"]
    assert not any(e.get("planar") for e in objects.values() if e["class"] not in policy.PLANAR_CLASSES)
    assert not any("planar" in e for e in leg["entries"] if e["kind"] == "material")
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_a_post_that_times_out_is_reposted_and_known_is_success(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    known = {"packet_id": "set-below", "status": "known"}
    daemon.post_script = [(202, {"status": "ingesting"}, 1.2), (200, known, 0.0)]
    [result] = send(["CAM_front"], packets, cfg, post_timeout_s=0.3)
    assert result.ok, (result.stage, result.errors)
    assert result.status_code == 200 and result.stage == "done" and result.ingest == "ingested"
    assert len(daemon.posted) == 2 and daemon.posted[0]["packet_path"] == daemon.posted[1]["packet_path"]
    assert any("timed out" in w and "re-post" in w for w in result.warnings)


def test_a_repost_that_is_refused_is_an_error_naming_its_status(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    daemon.post_script = [
        (202, {"status": "ingesting"}, 1.2),
        (422, {"errors": ["manifest.json missing"]}, 0.0),
    ]
    [result] = send(["CAM_front"], packets, cfg, post_timeout_s=0.3)
    assert not result.ok and result.stage == "post"
    assert result.errors[0].startswith("post: daemon: timed out; re-post answered 422")


def test_a_202_whose_ingest_fails_is_reported(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    daemon.post_body = {"packet_id": "x", "status": "ingesting", "events_url": "/api/v0/packets/x/events"}
    daemon.events = [
        {"seq": 1, "type": "ingest.file", "data": {"relpath": "beauty.exr"}},
        {"seq": 2, "type": "ingest.failed", "data": {"errors": ["files.beauty_exr: blake3 mismatch"]}},
    ]
    [result] = send(["CAM_front"], packets, cfg)
    assert result.status_code == 202 and result.events_url == "/api/v0/packets/x/events"
    assert result.ingest.startswith("failed") and "blake3 mismatch" in result.ingest
    assert not result.ok and result.stage == "ingest" and result.code == send_packet.INGEST_FAILED
    assert daemon.event_requests[0].startswith("/api/v0/packets/x/events?")


def test_an_ingest_still_running_after_the_window_is_ingesting(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    daemon.events = [{"seq": 1, "type": "ingest.file", "data": {"relpath": "beauty.exr"}}]
    [result] = send(["CAM_front"], packets, cfg, ingest_follow_s=0.0)
    assert result.ok and result.ingest == "ingesting" and len(daemon.event_requests) == 1


def test_post_timeout_grows_with_the_packet(monkeypatch, tmp_path):
    monkeypatch.setattr(transport, "folder_bytes", lambda folder: 2 * transport.GIB)
    assert transport.post_timeout_s(tmp_path) == pytest.approx(60.0)


# --------------------------------------------------------------------------- exr_header


def write_crypto_exr(path: Path, width: int, height: int, compression: str, coverage_dtype=None):
    """An EXR with an RGBA beauty, a depth pass and one Cryptomatte layer whose rank-0 ids form
    vertical bands of four names, with fractional coverage on alternate rows."""
    rng = np.random.default_rng(7)
    names = ["ARCH_wall", "VEG_tree", "SKY_dome", "PEOPLE_01"]
    band = np.arange(width) * len(names) // width
    ids = np.array([int(hex_id(n), 16) for n in names], np.uint32)[band]
    ids = np.broadcast_to(ids, (height, width)).copy()
    cov = np.ones((height, width), np.float32)
    cov[1::2] = 0.25
    cov[:, :3] = 0.0  # a few uncovered pixels
    channels = {
        "R": rng.random((height, width)).astype(np.float16),
        "G": rng.random((height, width)).astype(np.float16),
        "B": rng.random((height, width)).astype(np.float16),
        "A": np.ones((height, width), np.float16),
        "VRayZDepth.Z": rng.random((height, width)).astype(np.float32),
        "CryptoObject00.R": ids.view(np.float32),
        "CryptoObject00.G": cov.astype(coverage_dtype or np.float32),
        "CryptoObject00.B": rng.random((height, width)).astype(np.float32),
        "CryptoObject00.A": rng.random((height, width)).astype(np.float32),
        "CryptoObject01.R": rng.random((height, width)).astype(np.float32),
    }
    key = crypto_key("CryptoObject")
    header = {
        "compression": COMPRESSIONS[compression],
        "type": OpenEXR.scanlineimage,
        f"cryptomatte/{key}/name": "CryptoObject",
        f"cryptomatte/{key}/hash": "MurmurHash3_32",
        f"cryptomatte/{key}/conversion": "uint32_to_float32",
        f"cryptomatte/{key}/manifest": json.dumps({n: hex_id(n) for n in names}),
    }
    OpenEXR.File(header, channels).write(str(path))
    expected: dict[str, float] = {}
    for bits, c in zip(ids.ravel(), cov.ravel(), strict=True):
        if c > 0:
            expected[f"{int(bits):08x}"] = expected.get(f"{int(bits):08x}", 0.0) + float(c)
    return {k: v / (width * height) for k, v in expected.items()}


@pytest.mark.parametrize("compression", ["none", "zips", "zip"])
@pytest.mark.parametrize("size", [(37, 21), (64, 40)])
def test_rank0_coverage_matches_a_numpy_decode(tmp_path, compression, size):
    path = tmp_path / "c.exr"
    expected = write_crypto_exr(path, *size, compression)
    header = exr_header.read_header(path)
    assert header.compression_name == compression and (header.width, header.height) == size
    cov = exr_header.rank0_coverage(path, "CryptoObject", header)
    assert cov.compression == compression and cov.pixels == size[0] * size[1]
    assert set(cov.shares) == set(expected)
    for key, share in expected.items():
        assert cov.shares[key] == pytest.approx(share, abs=1e-9)


def test_half_float_coverage_is_read(tmp_path):
    path = tmp_path / "h.exr"
    expected = write_crypto_exr(path, 40, 18, "zip", coverage_dtype=np.float16)
    cov = exr_header.rank0_coverage(path, "CryptoObject")
    assert cov.shares == pytest.approx(expected)


def test_unsupported_storage_is_reported(tmp_path):
    path = tmp_path / "p.exr"
    write_crypto_exr(path, 40, 18, "piz")
    with pytest.raises(exr_header.CoverageUnsupported, match="piz"):
        exr_header.rank0_coverage(path, "CryptoObject")
    with pytest.raises(exr_header.CoverageUnsupported, match="CryptoMaterial00"):
        exr_header.rank0_coverage(path, "CryptoMaterial")


def test_header_reads_channels_types_and_the_cryptomatte_manifest(tmp_path):
    path = tmp_path / "c.exr"
    write_crypto_exr(path, 20, 10, "zip")
    header = exr_header.read_header(path)
    types = {ch.name: ch.dtype for ch in header.channels}
    assert types["R"] == "half" and types["VRayZDepth.Z"] == "float" and types["CryptoObject00.R"] == "float"
    groups = exr_header.layer_channels(header)
    assert [c.suffix for c in groups["VRayZDepth"]] == ["Z"] and {c.name for c in groups[""]} == set("RGBA")
    layers = exr_header.cryptomatte_layers(header)
    assert layers["CryptoObject"].ok and layers["CryptoObject"].manifest["SKY_dome"] == hex_id("SKY_dome")
    assert layers["CryptoObject"].key == crypto_key("CryptoObject")
    assert header.parts[0].data_window == (0, 0, 19, 9)


def test_multipart_files_are_read_part_by_part(tmp_path):
    path = tmp_path / "m.exr"
    ids = np.full((6, 8), int(hex_id("VEG_tree"), 16), np.uint32)
    ids[:, :2] = int(hex_id("SKY_dome"), 16)
    cov = np.ones((6, 8), np.float32)
    rgba = OpenEXR.Part(
        {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage},
        {"R": np.zeros((6, 8), np.float16)},
        name="rgba",
    )
    crypto = OpenEXR.Part(
        {
            "compression": OpenEXR.ZIPS_COMPRESSION,
            "type": OpenEXR.scanlineimage,
            "cryptomatte/abcdef0/name": "CryptoObject",
            "cryptomatte/abcdef0/manifest": json.dumps({"VEG_tree": hex_id("VEG_tree")}),
        },
        {"CryptoObject00.R": ids.view(np.float32), "CryptoObject00.G": cov},
        name="crypto",
    )
    OpenEXR.File([rgba, crypto]).write(str(path))
    header = exr_header.read_header(path)
    assert header.multipart and len(header.parts) == 2 and header.parts[1].name == "crypto"
    cov_ = exr_header.rank0_coverage(path, "CryptoObject", header)
    assert cov_.shares == pytest.approx({hex_id("VEG_tree"): 36 / 48, hex_id("SKY_dome"): 12 / 48})
    layers = exr_header.cryptomatte_layers(header)
    assert layers["CryptoObject"].manifest == {"VEG_tree": hex_id("VEG_tree")}


def test_manifest_problems_are_named():
    attrs = {
        "cryptomatte/1111111/name": "CryptoObject",
        "cryptomatte/1111111/hash": "md5",
        "cryptomatte/1111111/manifest": "{not json",
        "cryptomatte/2222222/name": "CryptoMaterial",
        "cryptomatte/2222222/manif_file": "CryptoMaterial.json",
    }
    layers = exr_header.cryptomatte_layers(attrs)
    assert not layers["CryptoObject"].ok
    assert any("md5" in p for p in layers["CryptoObject"].problems)
    assert any("not JSON" in p for p in layers["CryptoObject"].problems)
    assert any("sidecar" in p for p in layers["CryptoMaterial"].problems)


def test_not_an_exr_and_a_truncated_header_raise(tmp_path):
    bad = tmp_path / "x.exr"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    with pytest.raises(exr_header.ExrHeaderError, match="magic"):
        exr_header.read_header(bad)
    good = tmp_path / "c.exr"
    write_crypto_exr(good, 8, 4, "zip")
    cut = tmp_path / "cut.exr"
    cut.write_bytes(good.read_bytes()[:60])
    with pytest.raises(exr_header.ExrHeaderError):
        exr_header.read_header(cut)


def test_room_a_fixture_coverage_equals_its_legend():
    """The Blender fixture (fixtures/packets/room_a, written by OpenEXR) read with the stdlib parser."""
    exr = ROOM_A / "beauty.exr"
    if not exr.is_file():
        pytest.skip("fixtures/packets/room_a not built")
    header = exr_header.read_header(exr)
    assert header.compression_name == "zip"
    layers = exr_header.cryptomatte_layers(header)
    assert layers["CryptoObject"].ok and layers["CryptoMaterial"].ok
    cov = exr_header.rank0_coverage(exr, "CryptoObject", header)
    leg = read_json(ROOM_A / "id_legend.json")
    for entry in leg["entries"]:
        if entry["kind"] == "object":
            assert cov.shares[entry["id"][2:]] == pytest.approx(entry["pixel_share"], abs=6e-5)
    assert math.isclose(
        cov.covered,
        read_json(ROOM_A / "manifest.json")["id_legend"]["coverage"]["labelled_share"],
        abs_tol=2e-3,
    )
