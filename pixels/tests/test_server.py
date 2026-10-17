"""The pixels worker over HTTP (FastAPI TestClient and one real subprocess), the stub contracts and Tier C packets."""

from __future__ import annotations

import ast
import importlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import OpenEXR
import pytest
from fastapi.testclient import TestClient

import plate_spec
from plate_pixels import imageio
from plate_pixels.backends import (
    Backends,
    InputError,
    StubDepth,
    StubIntrinsics,
    StubSegment,
    kmeans_labels,
    pinhole_from_fov,
    segments_from_labels,
)
from plate_pixels.licences import REGISTRY
from plate_pixels.nvml import Admission
from plate_pixels.server import (
    PLATE_UPSCALE_ERROR,
    PORT,
    TokenUnavailable,
    cache_dir,
    configured_port,
    create_app,
    resolve_token,
)
from plate_pixels.tierc import ConsentRequired, build_tier_c_packet, new_packet_id

from .conftest import BASE_URL, TOKEN

PIXELS = Path(__file__).resolve().parent.parent
PKG = PIXELS / "plate_pixels"
HEIGHT, WIDTH = 180, 320


@pytest.fixture
def plate(tmp_path) -> str:
    """A 320 x 180 numpy-built 'client plate' (gradients plus a few blocks so k-means has structure)."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    rgb = np.stack([xx / WIDTH, yy / HEIGHT, (xx + yy) / (WIDTH + HEIGHT)], axis=-1).astype(np.float32)
    rgb[20:60, 30:110] = (0.9, 0.2, 0.2)
    rgb[100:160, 150:300] = (0.1, 0.6, 0.3)
    path = tmp_path / "client-plate.png"
    imageio.write_png8(path, rgb)
    return str(path)


def read_channels(path: str) -> dict[str, np.ndarray]:
    return {
        name: ch.pixels for name, ch in OpenEXR.File(str(path), separate_channels=True).channels().items()
    }


# --------------------------------------------------------------------------- stub mode is the default


def test_importing_the_server_does_not_import_torch():
    importlib.import_module("plate_pixels.server")
    assert "torch" not in sys.modules


def test_no_module_imports_torch_at_all():
    """Availability is probed with find_spec; no source file may import torch/torchvision itself."""
    bad = []
    for path in sorted(PKG.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad += [f"{path.name}: {n}" for n in names if n.split(".")[0] in {"torch", "torchvision"}]
    assert not bad, bad


def test_health_lists_every_registry_model_and_the_stub_state(client):
    r = client.post("/health")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["ok"] is True and body["stub_mode"] is True and body["torch"] is False
    assert body["real_required"] is False
    assert set(body["models"]) == {e.name for e in REGISTRY}
    assert body["models"]["depth-anything-v2-small"] == {
        "available": False,
        "licence": "Apache-2.0",
        "commercial_ok": True,
    }
    assert body["models"]["depth-anything-v2-large"]["commercial_ok"] is False
    assert body["models"]["opencv-dis"]["available"] is True  # real everywhere, no weights
    assert body["backends"]["flow"] == {"model": "opencv-dis", "impl": "dis", "stub": False}
    assert body["backends"]["depth"] == {"model": "depth-anything-v2-small", "impl": "stub", "stub": True}


def test_health_is_open_and_answers_get_too(make_client):
    anon = TestClient(create_app(TOKEN), base_url=BASE_URL)
    assert anon.post("/health").status_code == 200
    assert anon.get("/health").json()["ok"] is True


# --------------------------------------------------------------------------- auth, host and origin


@pytest.mark.parametrize(
    "path", ["/depth", "/intrinsics", "/segment", "/flow", "/ocr", "/tierc", "/admission"]
)
def test_every_route_but_health_needs_the_token(path):
    anon = TestClient(create_app(TOKEN), base_url=BASE_URL)
    r = anon.post(path, json={})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong-token-wrong-token-wrong-t", f"Basic {TOKEN}", "Bearer", TOKEN, f"Bearer {TOKEN}x", ""],
)
def test_a_wrong_or_malformed_credential_is_401(header):
    c = TestClient(create_app(TOKEN), base_url=BASE_URL, headers={"Authorization": header})
    assert c.post("/admission", json={}).status_code == 401


def test_unauthenticated_unknown_routes_are_401_not_404():
    anon = TestClient(create_app(TOKEN), base_url=BASE_URL)
    assert anon.post("/no-such-route").status_code == 401


def test_a_foreign_host_header_is_403(client):
    r = client.post("/health", headers={"Host": "evil.example:4611"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden_host"}
    assert client.post("/health", headers={"Host": "127.0.0.1:4610"}).status_code == 403  # the daemon's port
    assert client.post("/health", headers={"Host": "localhost:4611"}).status_code == 200


def test_a_browser_origin_is_403_even_with_the_token(client):
    r = client.post("/admission", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden_origin"}
    assert client.post("/health", headers={"Origin": "http://127.0.0.1:4611"}).status_code == 403


def test_the_allowed_host_follows_the_port(make_client):
    c = TestClient(
        create_app(TOKEN, port=4699),
        base_url="http://127.0.0.1:4699",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert c.post("/health").status_code == 200
    assert make_client(create_app(TOKEN, port=4699)).post("/health").status_code == 403  # Host says :4611


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        create_app("")


# --------------------------------------------------------------------------- the shared token file


def test_token_file_wins_over_config(tmp_path, monkeypatch):
    tokfile = tmp_path / "token.txt"
    tokfile.write_text("file-token-0123456789abcdef", encoding="utf-8", newline="\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"token": "config-token"}), encoding="utf-8", newline="\n")
    assert (
        resolve_token({"PLATE_TOKEN_FILE": str(tokfile), "PLATE_CONFIG": str(cfg)})
        == "file-token-0123456789abcdef"
    )


def test_token_file_with_a_bom_and_newline(tmp_path):
    tokfile = tmp_path / "token.txt"
    tokfile.write_bytes(b"\xef\xbb\xbfbom-token-0123456789abcdef\r\n")
    assert resolve_token({"PLATE_TOKEN_FILE": str(tokfile)}) == "bom-token-0123456789abcdef"


def test_a_short_or_missing_token_file_is_refused(tmp_path):
    short = tmp_path / "short.txt"
    short.write_text("abc", encoding="utf-8", newline="\n")
    with pytest.raises(TokenUnavailable, match="at least 16"):
        resolve_token({"PLATE_TOKEN_FILE": str(short)})
    with pytest.raises(TokenUnavailable, match="cannot read"):
        resolve_token({"PLATE_TOKEN_FILE": str(tmp_path / "absent.txt")})


def test_config_json_token_is_read_from_plate_config_or_plate_home(tmp_path):
    cfg = tmp_path / "elsewhere.json"
    cfg.write_text(
        json.dumps({"token": "from-config", "daemon_url": "http://127.0.0.1:4610"}),
        encoding="utf-8",
        newline="\n",
    )
    assert resolve_token({"PLATE_CONFIG": str(cfg)}) == "from-config"
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"token": "from-home"}), encoding="utf-8", newline="\n")
    assert resolve_token({"PLATE_HOME": str(home)}) == "from-home"


def test_no_token_anywhere_is_an_error_unless_creation_is_asked_for(tmp_path):
    env = {"PLATE_HOME": str(tmp_path / "fresh")}
    with pytest.raises(TokenUnavailable, match="no token"):
        resolve_token(env)
    assert not (tmp_path / "fresh").exists()  # reading never writes


def test_creating_the_token_keeps_the_plugins_keys_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.json"
    cfg.write_text(
        json.dumps({"repo_path": "D:/plate", "packets_dir": "D:/packets"}), encoding="utf-8", newline="\n"
    )
    env = {"PLATE_HOME": str(home)}
    token = resolve_token(env, create=True)
    assert len(token) >= 32
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved == {
        "repo_path": "D:/plate",
        "packets_dir": "D:/packets",
        "token": token,
        "daemon_url": "http://127.0.0.1:4610",
    }
    assert resolve_token(env, create=True) == token == resolve_token(env)
    assert b"\r" not in cfg.read_bytes()
    assert not list(home.glob("*.part"))


def test_creating_the_token_in_a_missing_folder(tmp_path):
    env = {"PLATE_HOME": str(tmp_path / "a" / "b")}
    token = resolve_token(env, create=True)
    assert json.loads((tmp_path / "a" / "b" / "config.json").read_text(encoding="utf-8"))["token"] == token


def test_a_corrupt_config_is_reported_not_overwritten(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json", encoding="utf-8", newline="\n")
    with pytest.raises(TokenUnavailable, match="not valid JSON"):
        resolve_token({"PLATE_CONFIG": str(cfg)}, create=True)
    assert cfg.read_text(encoding="utf-8") == "{not json"
    cfg.write_text("[1]", encoding="utf-8", newline="\n")
    with pytest.raises(TokenUnavailable, match="JSON object"):
        resolve_token({"PLATE_CONFIG": str(cfg)})


def test_the_daemon_and_the_worker_agree_on_the_token(tmp_path):
    """The daemon's Paths.token() and this worker read the same file to the same value."""
    Paths = pytest.importorskip("plate_daemon.config").Paths

    env = {"PLATE_HOME": str(tmp_path / "home")}
    paths = Paths.from_env(env)
    paths.home.mkdir(parents=True)
    assert resolve_token(env, create=True) == paths.token()


# --------------------------------------------------------------------------- routes, stub mode


def test_depth_writes_a_float_exr_and_says_it_is_a_stub(client, plate):
    r = client.post("/depth", json={"image": plate})
    assert r.status_code == 200
    body = r.json()
    assert body["stub"] is True and body["model"] == "depth-anything-v2-small" and body["impl"] == "stub"
    assert body["shape"] == [HEIGHT, WIDTH] and body["channel"] == "Z"
    assert body["depth_semantics"] == "relative_depth" and body["depth_is_metric"] is False
    z = read_channels(body["depth_exr"])["Z"]
    assert z.dtype == np.float32 and z.shape == (HEIGHT, WIDTH)
    expected = (1.0 / (1.0 + np.arange(HEIGHT, dtype=np.float32) / HEIGHT))[:, None]
    assert np.allclose(z, np.broadcast_to(expected, z.shape))
    assert body["min"] == pytest.approx(float(z.min())) and body["max"] == pytest.approx(1.0)


def test_depth_output_depends_on_the_input_path(client, tmp_path, plate):
    other = tmp_path / "other.png"
    imageio.write_png8(other, np.full((HEIGHT, WIDTH, 3), 0.5, np.float32))
    a = client.post("/depth", json={"image": plate}).json()["depth_exr"]
    b = client.post("/depth", json={"image": str(other)}).json()["depth_exr"]
    assert a != b
    assert client.post("/depth", json={"image": plate}).json()["depth_exr"] == a  # deterministic


def test_default_outputs_land_in_the_app_cache_and_out_overrides(client, plate, tmp_path):
    body = client.post("/depth", json={"image": plate}).json()
    out = Path(body["depth_exr"])
    assert out.parent == cache_dir() and out.parent.parent.name == "cache"
    assert out.name.startswith("depth-") and ":" not in out.name
    target = tmp_path / "mine" / "z.exr"
    body = client.post("/depth", json={"image": plate, "out": str(target)}).json()
    assert body["depth_exr"] == str(target) and target.is_file()
    assert not list((tmp_path / "mine").glob("*.part"))


def test_out_may_not_be_a_folder(client, plate, tmp_path):
    r = client.post("/depth", json={"image": plate, "out": str(tmp_path)})
    assert r.status_code == 400 and "folder" in r.json()["error"]


def test_intrinsics_is_a_36mm_60_degree_pinhole_flagged_stub(client, plate):
    body = client.post("/intrinsics", json={"image": plate}).json()
    assert body["stub"] is True and body["model"] == "geocalib"
    assert body["fov_deg"] == 60.0 and body["roll_deg"] == 0.0 and body["pitch_deg"] == 0.0
    assert body["cx"] == WIDTH / 2 and body["cy"] == HEIGHT / 2
    assert body["fx"] == body["fy"] == pytest.approx((WIDTH / 2) / np.tan(np.radians(30)))
    assert body["shape"] == [HEIGHT, WIDTH]


def test_segment_writes_a_16_bit_label_map_with_matching_segments(client, plate):
    body = client.post("/segment", json={"image": plate}).json()
    assert body["stub"] is True and body["model"] == "sam2.1" and body["shape"] == [HEIGHT, WIDTH]
    labels = imageio.cv2.imread(body["segments_png"], imageio.cv2.IMREAD_UNCHANGED)
    assert labels.dtype == np.uint16 and labels.shape == (HEIGHT, WIDTH)
    segs = body["segments"]
    assert 2 <= len(segs) <= 6 and {s["id"] for s in segs} == set(np.unique(labels).tolist()) - {0}
    assert sum(s["area"] for s in segs) == HEIGHT * WIDTH  # the stub covers every pixel
    for s in segs:
        x0, y0, x1, y1 = s["bbox"]
        assert 0 <= x0 < x1 <= WIDTH and 0 <= y0 < y1 <= HEIGHT
        assert int((labels == s["id"]).sum()) == s["area"]


def test_segment_accepts_prompt_points_and_rejects_bad_ones(client, plate):
    assert client.post("/segment", json={"image": plate, "points": [[10, 12], [20.5, 8]]}).status_code == 200
    for bad in ("nope", [[1]], [[1, 2, 3]], [["a", "b"]], 5):
        r = client.post("/segment", json={"image": plate, "points": bad})
        assert r.status_code == 400 and "points" in r.json()["error"], bad


def test_flow_is_real_dis_and_not_a_stub(client, tmp_path):
    from .test_flow_nvml import shifted, textured

    a = textured(HEIGHT, WIDTH)
    pa, pb = tmp_path / "a.png", tmp_path / "b.png"
    imageio.write_png16(pa, a)
    imageio.write_png16(pb, shifted(a, 2))
    body = client.post("/flow", json={"a": str(pa), "b": str(pb)}).json()
    assert body["stub"] is False and body["model"] == "opencv-dis" and body["impl"] == "dis"
    assert body["eval_width"] == 960 and body["channels"] == ["U", "V"]
    assert abs(abs(body["median_u"]) - 2.0) <= 0.3 and abs(body["median_v"]) <= 0.3
    assert abs(body["median_px"] - 2.0) <= 0.3
    ch = read_channels(body["flow_exr"])
    assert set(ch) == {"U", "V"} and ch["U"].shape == (HEIGHT, WIDTH)
    assert abs(float(np.median(ch["U"])) - body["median_u"]) < 1e-5


def test_flow_of_two_different_sizes_is_a_400(client, plate, png):
    r = client.post("/flow", json={"a": plate, "b": png})
    assert r.status_code == 400 and "one size" in r.json()["error"]


def test_ocr_is_empty_without_a_sidecar_and_reads_a_fixture_sidecar(client, png):
    body = client.post("/ocr", json={"image": png}).json()
    assert body["stub"] is True and body["model"] == "paddleocr" and body["boxes"] == []
    assert body["shape"] == [32, 48]
    boxes = [{"bbox": [1, 2, 20, 12], "text": "EXIT", "conf": 0.98}]
    Path(png + ".ocr.json").write_text(json.dumps(boxes), encoding="utf-8", newline="\n")
    assert client.post("/ocr", json={"image": png}).json()["boxes"] == boxes
    Path(png + ".ocr.json").write_text("{oops", encoding="utf-8", newline="\n")
    assert client.post("/ocr", json={"image": png}).status_code == 400


def test_admission_on_the_mac_is_refused_unless_unmanaged_is_allowed(make_client, monkeypatch):
    monkeypatch.setitem(sys.modules, "pynvml", None)  # no NVML here, whatever machine runs this
    c = make_client(create_app(TOKEN))
    body = c.post("/admission", json={"required_mb": 4000, "model": "depth-anything-v2-small"}).json()
    assert (
        body["ok"] is False
        and "no nvml" in body["reason"]
        and body["headroom_mb"] is None
        and body["nvml"] is False
    )
    allowed = make_client(create_app(TOKEN, admission=Admission(env={"PLATE_ALLOW_UNMANAGED_GPU": "1"})))
    body = allowed.post("/admission", json={"required_mb": 4000}).json()
    assert body["ok"] is True and body["reason"] == "no nvml: unmanaged" and body["unmanaged_allowed"] is True


def test_admission_route_applies_windows(make_client):
    from .test_flow_nvml import FakeNvml

    adm = Admission(nvml=FakeNvml(30000), windows="03:00-03:01", env={})
    c = make_client(create_app(TOKEN, admission=adm))
    body = c.post("/admission", json={"required_mb": 8000, "model": "qwen-image-edit-2509"}).json()
    assert (
        body["ok"] is False
        and "scheduled GPU window" in body["reason"]
        and body["windows"][0]["start_hhmm"] == "03:00"
    )
    assert (
        c.post("/admission", json={"required_mb": 8000, "model": "depth-anything-v2-small"}).json()["ok"]
        is True
    )
    assert c.post("/admission", json={"required_mb": -1}).status_code == 400


def test_upscale_refuses_the_plate_and_anything_not_declared_an_ai_layer(client, plate):
    for body in (
        {"image": plate, "scale": 2, "is_plate": True},
        {"image": plate, "scale": 2, "ai_layer": True, "is_plate": True},
        {"image": plate, "scale": 2},
        {"image": plate},
    ):
        r = client.post("/upscale", json=body)
        assert r.status_code == 400 and r.json() == {"error": PLATE_UPSCALE_ERROR}, body
    ok = client.post("/upscale", json={"image": plate, "scale": 2, "ai_layer": True})
    assert ok.status_code == 200 and ok.json()["stub"] is True and "never the plate" in ok.json()["note"]
    assert client.post("/upscale", json={"image": plate, "ai_layer": True, "scale": 0}).status_code == 400


def test_lpips_stub(client, plate):
    body = client.post("/lpips", json={"a": plate, "b": plate}).json()
    assert body["stub"] is True and body["model"] == "lpips" and body["distance"] is None


# --------------------------------------------------------------------------- errors


def test_unknown_route_is_a_json_404(client):
    r = client.post("/nope")
    assert r.status_code == 404 and r.json() == {"error": "unknown route /nope"}


def test_wrong_method_is_a_json_405(client):
    r = client.get("/depth")
    assert r.status_code == 405 and "error" in r.json()


def test_a_missing_field_is_a_400_naming_it(client):
    r = client.post("/depth", json={})
    assert r.status_code == 400 and r.json()["error"] == "invalid request: image: Field required"
    assert r.json()["detail"] == [{"field": "image", "problem": "Field required"}]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/depth", {"image": ""}),
        ("/depth", {"image": 5}),
        ("/depth", {"image": "x.png", "surprise": 1}),
        ("/flow", {"a": "x.png"}),
        ("/tierc", {"plate_path": "x.png"}),
        ("/admission", {"required_mb": "lots"}),
    ],
)
def test_bad_request_bodies_are_400(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 400 and "error" in r.json()


def test_not_json_and_not_an_object_are_400(client):
    assert (
        client.post("/depth", content=b"{not json", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    assert (
        client.post("/depth", content=b"[1, 2]", headers={"Content-Type": "application/json"}).status_code
        == 400
    )


def test_a_missing_or_unreadable_image_is_a_400(client, tmp_path):
    r = client.post("/depth", json={"image": str(tmp_path / "absent.png")})
    assert r.status_code == 400 and "cannot read image" in r.json()["error"]
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this is not a png")
    assert client.post("/segment", json={"image": str(junk)}).status_code == 400
    assert client.post("/ocr", json={"image": str(junk)}).status_code == 400


# --------------------------------------------------------------------------- real mode and admission wiring


def test_real_mode_makes_a_failed_import_a_500_on_that_route_only(make_client, plate, monkeypatch):
    monkeypatch.setenv("PLATE_PIXELS_REAL", "1")
    c = make_client(create_app(TOKEN))
    r = c.post("/depth", json={"image": plate})
    assert r.status_code == 500
    assert "PLATE_PIXELS_REAL=1" in r.json()["error"] and r.json()["real_required"] is True
    assert "depth-anything-v2-small" in r.json()["error"]
    health = c.post("/health").json()  # the worker itself stays up and says what it would use
    assert health["real_required"] is True and health["backends"]["depth"]["impl"] == "real"
    assert health["backends"]["flow"]["model"] == "raft"
    assert (
        c.post("/flow", json={"a": plate, "b": plate}).status_code == 500
    )  # RAFT is not DIS under the real flag
    assert c.post("/admission", json={}).status_code == 200


class FakeRealDepth(StubDepth):
    """A pretend real model: not a stub, wants 4000 MiB, counts its inferences."""

    is_stub = False
    impl = "fake-real"
    vram_mb = 4000
    runs = 0

    def infer(self, rgb):
        type(self).runs += 1
        return super().infer(rgb)


def test_a_real_backend_is_admitted_once_and_refused_when_vram_is_short(make_client, plate):
    from .test_flow_nvml import FakeNvml

    FakeRealDepth.runs = 0
    short = Admission(nvml=FakeNvml(3000), env={})
    c = make_client(create_app(TOKEN, backends=Backends(depth=FakeRealDepth()), admission=short))
    r = c.post("/depth", json={"image": plate})
    assert r.status_code == 503 and "insufficient VRAM" in r.json()["error"]
    assert FakeRealDepth.runs == 0  # refused before inference

    nvml = FakeNvml(9000)
    roomy = Admission(nvml=nvml, env={})
    c = make_client(create_app(TOKEN, backends=Backends(depth=FakeRealDepth()), admission=roomy))
    body = c.post("/depth", json={"image": plate}).json()
    assert body["stub"] is False and body["impl"] == "fake-real"
    assert c.post("/depth", json={"image": plate}).status_code == 200
    assert FakeRealDepth.runs == 2 and nvml.queries == 1  # admitted on the first load only


def test_stub_and_dis_backends_are_never_admission_checked(make_client, plate):
    from .test_flow_nvml import FakeNvml

    nvml = FakeNvml(0)
    c = make_client(create_app(TOKEN, admission=Admission(nvml=nvml, env={})))
    for path, body in (
        ("/depth", {"image": plate}),
        ("/flow", {"a": plate, "b": plate}),
        ("/segment", {"image": plate}),
    ):
        assert c.post(path, json=body).status_code == 200, path
    assert nvml.queries == 0


# --------------------------------------------------------------------------- the stub contracts


def test_stub_depth_is_one_over_one_plus_row_fraction():
    rgb = np.zeros((8, 5, 3), np.float32)
    z = StubDepth().infer(rgb)
    assert z.dtype == np.float32 and z.shape == (8, 5)
    assert np.allclose(z[:, 0], 1.0 / (1.0 + np.arange(8) / 8))
    assert np.all(z[:, 0] == z[:, 4]) and z[0, 0] == 1.0 and np.all(np.diff(z[:, 0]) < 0)


def test_stub_intrinsics_follow_the_60_degree_pinhole():
    k = StubIntrinsics().calibrate(np.zeros((90, 160, 3), np.float32))
    assert k == {**pinhole_from_fov(160, 90, 60.0), "roll_deg": 0.0, "pitch_deg": 0.0}
    assert k["fov_deg"] == 60.0 and k["fx"] == pytest.approx(80 / np.tan(np.radians(30)))
    assert (k["cx"], k["cy"]) == (80.0, 45.0)


def test_stub_segmentation_is_deterministic_six_label_kmeans(plate):
    from plate_pixels.backends import read_rgb

    rgb = read_rgb(plate)
    a, segs_a = StubSegment().segment(rgb, None)
    b, segs_b = StubSegment().segment(rgb, [[1, 1]])  # prompts are ignored by the stub
    assert a.dtype == np.uint16 and np.array_equal(a, b) and segs_a == segs_b
    assert set(np.unique(a).tolist()) <= set(range(1, 7))
    assert a.min() >= 1  # no zero label: every pixel belongs to a segment


def test_kmeans_labels_are_ordered_by_luminance_and_cover_a_flat_image():
    rgb = np.zeros((20, 20, 3), np.float32)
    rgb[:, 10:] = 1.0
    labels = kmeans_labels(rgb, k=2)
    assert labels[0, 0] < labels[0, 19] and set(np.unique(labels).tolist()) == {1, 2}
    flat = kmeans_labels(np.full((6, 6, 3), 0.4, np.float32))
    assert flat.shape == (6, 6) and flat.dtype == np.uint16


def test_segments_from_labels_boxes_are_half_open():
    labels = np.zeros((6, 8), np.uint16)
    labels[1:3, 2:5] = 7
    assert segments_from_labels(labels) == [{"id": 7, "bbox": [2, 1, 5, 3], "area": 6}]
    assert segments_from_labels(np.zeros((3, 3), np.uint16)) == []


def test_read_rgb_raises_input_error(tmp_path):
    from plate_pixels.backends import read_rgb

    with pytest.raises(InputError):
        read_rgb(tmp_path / "absent.png")


def test_configured_port(monkeypatch):
    assert configured_port() == PORT == 4611
    assert configured_port({"PLATE_PIXELS_PORT": "4699"}) == 4699
    for bad in ("abc", "0", "70000", "-1"):
        with pytest.raises(SystemExit):
            configured_port({"PLATE_PIXELS_PORT": bad})


# --------------------------------------------------------------------------- Tier C


def tierc_body(plate: str, out_dir: Path, **extra) -> dict:
    return {
        "plate_path": plate,
        "out_dir": str(out_dir),
        "project_id": "aadhya-serene",
        "shot_id": "lobby_02",
        "consent_record": "consent-2026-10-14.pdf",
        **extra,
    }


def test_tierc_route_builds_a_packet_that_validates(client, plate, tmp_path):
    r = client.post("/tierc", json=tierc_body(plate, tmp_path / "packet"))
    assert r.status_code == 200
    body = r.json()
    assert body["stub"] is True and body["tier"] == "C" and body["packet_id"].startswith("pkt_")
    packet = Path(body["packet_dir"])
    assert plate_spec.validate_packet_dir(packet) == []
    assert sorted(p.name for p in packet.iterdir()) == [
        "client_plate.png",
        "depth_estimated.exr",
        "id_legend.json",
        "manifest.json",
        "segments.png",
    ]
    for key in ("depth", "intrinsics", "segments"):
        assert (
            body["estimated"][key]["model"].startswith("stub-")
            and body["estimated"][key]["version"] == "stub"
        )
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["packet_id"] == body["packet_id"]
    assert manifest["tier"] == "C" and manifest["files"]["client_plate"]["width"] == WIDTH
    assert manifest["estimated"]["depth_is_metric"] is False
    assert manifest["estimated"]["client"]["consent_record"] == "consent-2026-10-14.pdf"
    assert manifest["camera"]["resolution"] == {"width": WIDTH, "height": HEIGHT, "pixel_aspect": 1.0}
    assert manifest["camera"]["film_width_mm"] == 36.0 and manifest["camera"]["source"] == "estimated"
    assert manifest["camera"]["fov_deg"]["horizontal"] == pytest.approx(60.0, abs=0.01)
    z = read_channels(str(packet / "depth_estimated.exr"))["Z"]
    assert z.shape == (HEIGHT, WIDTH) and z.dtype == np.float32
    legend = json.loads((packet / "id_legend.json").read_text(encoding="utf-8"))
    assert legend["entries"] and all(
        e["id"].startswith("s:") and e["class"] == "other" and e["class_source"] == "autotag"
        for e in legend["entries"]
    )
    assert manifest["policy"]["editable_ids"] == [] and set(manifest["policy"]["locked_ids"]) == {
        e["id"] for e in legend["entries"]
    }


def test_tierc_needs_a_consent_record(client, plate, tmp_path):
    body = tierc_body(plate, tmp_path / "packet")
    for consent in (None, "", "   "):
        sent = (
            {k: v for k, v in body.items() if k != "consent_record"}
            if consent is None
            else {**body, "consent_record": consent}
        )
        r = client.post("/tierc", json=sent)
        assert r.status_code == 428 and r.json() == {"error": "consent record required"}
    assert not (tmp_path / "packet").exists()  # refused before anything was written


def test_tierc_refuses_a_bad_slug_a_busy_folder_and_a_bad_plate(client, plate, tmp_path):
    assert (
        client.post(
            "/tierc", json={**tierc_body(plate, tmp_path / "p1"), "project_id": "Bad Slug"}
        ).status_code
        == 400
    )
    assert (
        client.post("/tierc", json={**tierc_body(plate, tmp_path / "p1"), "shot_id": "a/b"}).status_code
        == 400
    )
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x", encoding="utf-8", newline="\n")
    assert client.post("/tierc", json=tierc_body(plate, busy)).status_code == 409
    r = client.post("/tierc", json=tierc_body(str(tmp_path / "absent.png"), tmp_path / "p2"))
    assert r.status_code == 400 and "cannot read the plate" in r.json()["error"]


def test_tierc_labels_make_editable_ids_and_nda_pins_custody(client, plate, tmp_path):
    first = client.post("/tierc", json=tierc_body(plate, tmp_path / "p1")).json()
    legend = json.loads((Path(first["packet_dir"]) / "id_legend.json").read_text(encoding="utf-8"))
    ids = [int(e["id"][2:]) for e in legend["entries"]]
    body = tierc_body(
        plate,
        tmp_path / "p2",
        nda=True,
        labels={str(ids[0]): {"class": "vegetation", "name": "shrub"}, str(ids[1]): {"class": "glazing"}},
    )
    r = client.post("/tierc", json=body)
    assert r.status_code == 200
    packet = Path(r.json()["packet_dir"])
    assert plate_spec.validate_packet_dir(packet) == []
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["policy"]["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    assert manifest["policy"]["editable_ids"] == [f"s:{ids[0]:05d}"]  # glazing is never editable
    assert f"s:{ids[1]:05d}" in manifest["policy"]["locked_ids"]
    named = {
        e["id"]: e for e in json.loads((packet / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    }
    assert (
        named[f"s:{ids[0]:05d}"]["name"] == "shrub" and named[f"s:{ids[0]:05d}"]["class_source"] == "manual"
    )
    bad = client.post("/tierc", json=tierc_body(plate, tmp_path / "p3", labels={"999": {"class": "sky"}}))
    assert bad.status_code == 400 and "999" in bad.json()["error"]
    bad = client.post(
        "/tierc", json=tierc_body(plate, tmp_path / "p4", labels={str(ids[0]): {"class": "banana"}})
    )
    assert bad.status_code == 400 and "banana" in bad.json()["error"]


def test_tierc_keeps_a_16_bit_plate_16_bit(tmp_path):
    rgb = np.random.default_rng(2).random((HEIGHT, WIDTH, 3), dtype=np.float32)
    src = tmp_path / "plate16.png"
    imageio.write_png16(src, rgb)
    packet = build_tier_c_packet(src, tmp_path / "pkt", Backends(), "proj", "shot", "consent-1")
    assert plate_spec.validate_packet_dir(packet) == []
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["client_plate"]["bit_depth"] == 16
    assert imageio.read(packet / "client_plate.png").bit_depth == 16


def test_tierc_hashes_the_original_file_name_not_the_name_itself(client, plate, tmp_path):
    packet = Path(client.post("/tierc", json=tierc_body(plate, tmp_path / "p")).json()["packet_dir"])
    text = (packet / "manifest.json").read_text(encoding="utf-8")
    assert "client-plate.png" not in text and "original_filename_hash" in text


def test_build_tier_c_packet_direct_errors(tmp_path, plate):
    with pytest.raises(ConsentRequired):
        build_tier_c_packet(plate, tmp_path / "a", Backends(), "proj", "shot", "")
    with pytest.raises(InputError):
        build_tier_c_packet(tmp_path / "absent.png", tmp_path / "b", Backends(), "proj", "shot", "consent")


def test_packet_ids_are_ulids_in_time_order():
    a, b = new_packet_id(1_000), new_packet_id(2_000)
    assert a.startswith("pkt_") and len(a) == 30 and a[4:] < b[4:]
    assert len({new_packet_id(1_000) for _ in range(50)}) == 50


def test_a_stub_segmentation_is_never_presented_as_a_measurement(tmp_path, plate):
    packet = build_tier_c_packet(plate, tmp_path / "pkt", Backends(), "proj", "shot", "consent")
    est = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))["estimated"]
    assert est["segments"]["model"] == "stub-sam2.1" and est["segments"]["version"] == "stub"
    assert est["depth"]["model"] == "stub-depth-anything-v2-small"


# --------------------------------------------------------------------------- a real process


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(module: str, tmp_path: Path, port: str, token_file: Path | None) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(PIXELS), str(PIXELS.parent / "spec")]),
        "PLATE_PIXELS_PORT": port,
        "PLATE_MODELS": str(tmp_path / "models"),
        "PLATE_HOME": str(tmp_path / "home"),
    }
    for name in ("PLATE_PIXELS_REAL", "PLATE_TOKEN_FILE", "PLATE_CONFIG"):
        env.pop(name, None)
    if token_file is not None:
        env["PLATE_TOKEN_FILE"] = str(token_file)
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("module", ["plate_pixels.server", "plate_pixels"])
def test_python_dash_m_serves_authed_stub_routes(tmp_path, plate, module):
    """`python -m plate_pixels.server` (the Makefile target) and `python -m plate_pixels` over real HTTP."""
    token = "process-token-0123456789abcdef"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8", newline="\n")
    port = free_port()
    proc = spawn(module, tmp_path, str(port), token_file)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                health = httpx.post(f"{base}/health", timeout=5)
                break
            except httpx.TransportError:
                assert proc.poll() is None, proc.stderr.read()
                assert time.monotonic() < deadline, "server did not come up"
                time.sleep(0.2)
        assert (
            health.status_code == 200
            and health.json()["stub_mode"] is True
            and health.json()["torch"] is False
        )
        assert httpx.post(f"{base}/depth", json={"image": plate}, timeout=10).status_code == 401
        ok = httpx.post(
            f"{base}/depth", json={"image": plate}, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        assert ok.status_code == 200 and ok.json()["stub"] is True
        assert Path(ok.json()["depth_exr"]).is_file()
        assert httpx.post(f"{base}/health", headers={"Host": "evil.example"}, timeout=5).status_code == 403
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()


def test_the_process_prints_its_url_and_creates_the_shared_token(tmp_path):
    port = free_port()
    proc = spawn("plate_pixels.server", tmp_path, str(port), None)
    try:
        line = proc.stdout.readline()
        assert line.startswith(f"PLATE pixels http://127.0.0.1:{port}") and "stub_mode=True" in line
        cfg = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
        assert len(cfg["token"]) >= 32 and cfg["daemon_url"] == "http://127.0.0.1:4610"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()


def test_a_bad_port_exits_with_a_message(tmp_path):
    proc = spawn("plate_pixels", tmp_path, "abc", None)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode != 0 and "PLATE_PIXELS_PORT must be an integer" in err
