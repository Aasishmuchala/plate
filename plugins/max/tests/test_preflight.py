"""The Send preflight: the daemon's token, its packets folders and the payload limit are checked before
anything renders, so a wrong token, a wrong folder or an oversize payload never costs a V-Ray render.

Uses the stub daemon and simulated V-Ray of test_send_packet (imported helpers only, so no test is
collected twice)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plate_max import transport
from plate_max.actions import batch_cameras, send_packet
from plate_max.bridge import colour, hashing

pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, gamma_colour, send, serve_daemon, vray_scene  # noqa: E402


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


# --------------------------------------------------------------------------- the token


def test_a_bad_token_fails_at_preflight_with_no_render(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, vray = vray_scene(install_scene)
    results = send(["CAM_front", "CAM_aerial"], packets, {**cfg, "token": "wrong" + cfg["token"]})
    assert [r.ok for r in results] == [False, False]
    for r in results:
        assert r.stage == "preflight" and r.code == send_packet.PREFLIGHT_TOKEN
        assert r.errors[0].startswith("daemon rejected the token")
    assert scene.render_calls == [] and vray.written == []
    assert list(packets.iterdir()) == [] and daemon.hasher.requests == [] and daemon.posted == []
    assert daemon.session_calls == 1  # checked once for the whole Send


def test_no_token_fails_at_preflight_without_asking_the_daemon(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    [result] = send(["CAM_front"], packets, {**cfg, "token": ""})
    assert result.stage == "preflight" and result.code == send_packet.PREFLIGHT_TOKEN
    assert "no token" in result.errors[0]
    assert scene.render_calls == [] and daemon.session_calls == 0


def test_a_daemon_without_a_session_route_is_refused(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    daemon.session_status = 404
    [result] = send(["CAM_front"], packets, cfg)
    assert result.stage == "preflight" and result.code == send_packet.PREFLIGHT_SESSION
    assert "404" in result.errors[0] and scene.render_calls == []


# --------------------------------------------------------------------------- the packets folder


def test_an_out_root_outside_the_packets_dirs_fails_at_preflight(install_scene, daemon_env, tmp_path):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    elsewhere = tmp_path / "Desktop" / "renders"
    [result] = send(["CAM_front"], elsewhere, cfg)
    assert result.stage == "preflight" and result.code == send_packet.PREFLIGHT_OUT_ROOT
    assert result.errors[0] == f"{elsewhere} is outside the daemon's packets folders: {packets}"
    assert scene.render_calls == [] and daemon.hasher.requests == []
    assert not elsewhere.exists()


def test_a_sibling_with_a_shared_prefix_is_not_inside(install_scene, daemon_env):
    """packets-old starts with the text "packets" but is not inside it."""
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    [result] = send(["CAM_front"], packets.parent / (packets.name + "-old"), cfg)
    assert result.code == send_packet.PREFLIGHT_OUT_ROOT and scene.render_calls == []


def test_a_subfolder_of_a_packets_dir_passes(install_scene, daemon_env):
    _, cfg, packets = daemon_env
    vray_scene(install_scene)
    sub = packets / "tula-wot"
    [result] = send(["CAM_front"], sub, cfg)
    assert result.ok, (result.stage, result.errors)
    assert Path(result.packet_dir).parent == sub


def test_a_daemon_that_names_no_packets_dirs_refuses_every_folder(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    daemon.packets_dirs = []
    [result] = send(["CAM_front"], packets, cfg)
    assert result.code == send_packet.PREFLIGHT_OUT_ROOT and "names none" in result.errors[0]
    assert scene.render_calls == []


def test_daemon_gate_still_unpacks_as_ok_and_reason(daemon_env):
    """send_motion calls daemon_gate(config) and unpacks two values: that keeps working."""
    _, cfg, packets = daemon_env
    ok, reason = send_packet.daemon_gate(cfg)
    assert (ok, reason) == (True, "")
    gate = send_packet.daemon_gate(cfg, packets)
    assert gate.ok and gate.code == "" and gate.session["packets_dirs"] == [str(packets)]
    bad = send_packet.daemon_gate({**cfg, "token": "nope"})
    assert tuple(bad) == (False, bad.reason) and bad.code == send_packet.PREFLIGHT_TOKEN
    assert bad.session is None


# --------------------------------------------------------------------------- Batch Cameras


def test_batch_cameras_reruns_the_preflight_for_every_camera(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    seen: list[str] = []

    def progress(_done, _total, message):
        seen.append(message)
        if message.startswith("CAM_front: sent"):
            daemon.hasher.token = "rotated-" + cfg["token"]  # the daemon restarted with a new token

    opts = {"config": cfg, "project_id": "tula-wot", "progress": progress}
    results = batch_cameras.run(["CAM_front", "CAM_aerial", "CAM_entry"], packets, cfg["daemon_url"], opts)
    assert [r.ok for r in results] == [True, False, False]
    assert [r.code for r in results[1:]] == [send_packet.PREFLIGHT_TOKEN] * 2
    assert all(r.stage == "preflight" for r in results[1:])
    assert daemon.session_calls == 3 and daemon.health_calls == 3
    assert len(daemon.posted) == 1  # only the first camera rendered and posted


# --------------------------------------------------------------------------- payloads


def test_an_oversize_legend_record_is_refused_before_the_render(install_scene, daemon_env, monkeypatch):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    monkeypatch.setattr(hashing, "PAYLOAD_LIMIT", 120)  # a clubhouse node record is 150-200 bytes
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "preflight" and result.code == send_packet.PAYLOAD_TOO_LARGE
    assert result.errors[0].startswith("PAYLOAD_TOO_LARGE: payload 'legend record of ")
    assert "bytes, over the 120-byte cap" in result.errors[0]
    assert scene.render_calls == [] and list(packets.iterdir()) == []


def test_hash_payload_bytes_refuses_over_the_limit_before_sending(daemon_env, monkeypatch):
    daemon, cfg, _ = daemon_env
    monkeypatch.setattr(hashing, "PAYLOAD_LIMIT", 16)
    with pytest.raises(hashing.PayloadTooLarge) as info:
        hashing.hash_payload_bytes({"small": b"x", "view": b"v" * 17}, cfg)
    assert info.value.code == "PAYLOAD_TOO_LARGE" and info.value.name == "view" and info.value.size == 17
    assert isinstance(info.value, ValueError) and daemon.hasher.requests == []  # nothing half-sent


def test_big_sidecars_scene_digest_and_view_go_by_path(daemon_env, monkeypatch):
    daemon, cfg, packets = daemon_env
    monkeypatch.setattr(hashing, "PAYLOAD_LIMIT", 64)
    folder = packets / "pkt_big"
    folder.mkdir()
    legend_doc = {
        "packet_id": "pkt_big",
        "entries": [{"id": f"o:{i:08x}", "name": "n" * 40} for i in range(5)],
    }
    out = hashing.sidecar_hashes(cfg, legend=legend_doc, frames_bytes=b"f" * 500, folder=folder)
    assert set(out) == {"id_legend.json", "camera_frames.json"}
    assert (folder / "camera_frames.json").read_bytes() == b"f" * 500
    scene = {"digest_version": "1", "lines": [f"node|n{i}|L|M" for i in range(50)]}
    hashes: dict = {}
    block = hashing.hash_scene(scene, cfg, folder=folder, hashes=hashes)
    assert hashes["scene_digest.txt"]["hex"] == block["hex"] and block["digest_version"] == "1"
    ref = hashing.scene_digest_ref(hashes)
    assert ref["path"] == "scene_digest.txt" and ref["bytes"] == (folder / "scene_digest.txt").stat().st_size
    view = hashing.hash_view(b"v" * 100, cfg, folder=folder)
    assert len(view["hex"]) == 64 and not (folder / hashing.VIEW_PAYLOAD_TEMP).exists()
    assert all(not body.get("payloads") for body in daemon.hasher.requests)
    with pytest.raises(hashing.PayloadTooLarge):
        hashing.hash_view(b"v" * 100, cfg)  # no folder to fall back on


# --------------------------------------------------------------------------- colour


def _ocio(scene, path):
    scene.ColorPipelineMgr = SimpleNamespace(
        Mode="OCIO", OCIOConfigPath=str(path), displayName="sRGB", viewName="ACES 1.0 SDR-video"
    )


def test_the_ocio_hash_lets_unauthorized_through(scene, daemon_env, tmp_path):
    _, cfg, _ = daemon_env
    config = tmp_path / "studio.ocio"
    config.write_bytes(b"ocio_profile_version: 2\n")
    _ocio(scene, config)
    with pytest.raises(transport.Unauthorized):
        colour.detect_colour_pipeline({**cfg, "token": "wrong"})
    assert colour.detect_colour_pipeline(cfg)["mode"] == "ocio"


def test_other_hash_failures_leave_the_config_unhashed(scene, tmp_path, monkeypatch):
    config = tmp_path / "studio.ocio"
    config.write_bytes(b"ocio_profile_version: 2\n")
    _ocio(scene, config)

    def boom(*_a, **_k):
        raise transport.UnexpectedStatus(500, {"error": "boom"})

    monkeypatch.setattr(colour, "hash_payload_bytes", boom)
    out = colour.detect_colour_pipeline({"token": "t", "daemon_url": "http://127.0.0.1:9"})
    assert out["mode"] == "unknown" and out["config_hash"] is None and "config_hash" in out["reason"]


def test_an_oversize_ocio_config_blocks_the_send_by_name(install_scene, daemon_env, tmp_path, monkeypatch):
    _, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene, colour=False)
    big = tmp_path / "huge.ocio"
    big.write_bytes(b"#" * 300)
    _ocio(scene, big)
    monkeypatch.setattr(colour, "CONFIG_PAYLOAD_LIMIT", 256)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "colour"
    assert "PAYLOAD_TOO_LARGE" in result.errors[0] and "300 bytes" in result.errors[0]
    assert scene.render_calls == []
    gamma_colour(scene)  # the same scene in gamma mode sends
    [again] = send(["CAM_front"], packets, cfg)
    assert again.ok, again.errors
