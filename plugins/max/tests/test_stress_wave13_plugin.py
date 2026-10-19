"""Wave 13 stress fixes (T158): a daemon failure after the render takes the folder with it, and a long
scene name no longer breaks the project binding.

  t9_oneshot            a /hash that answers 503 past T142's retries right after the render: the Send
                        fails at "legend" and the manifest-less packet folder is removed (decision 92);
                        keep_failed_packets keeps it, a dropped connection or a timeout behaves the
                        same, and the unlabelled-share abort (a scene problem) still keeps its render
  long-scene-name       a 249- or 254-character .max stem: the sidecar lives under
                        <app data>/sidecars/<hash>.plate.json, the project binding and the recipes
                        round-trip through it, Send proceeds, the backup goes to the temp folder; a
                        200-character stem stays beside the scene; Windows' 259-character path limit
                        is a hook (platform_is_windows, WINDOWS_MAX_PATH), not a monkeypatched os.name
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from plate_max import policy, recipes, transport
from plate_max.actions import send_packet
from plate_max.bridge import safety
from plate_max.recipes import CameraRecipe, SidecarUnwritable
from plate_max.ui import controller as ctl

pytest.importorskip("numpy")
pytest.importorskip("OpenEXR")

from test_send_packet import Daemon, FakeVRay, gamma_colour, send, serve_daemon, vray_scene  # noqa: E402


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    """Journals, sidecars and backups go under the test's folders, never the real app data or /tmp."""
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(transport, "_sleep", lambda s: None)  # /hash retries do not wait


@pytest.fixture
def daemon_env(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def packet_folders(packets: Path) -> list[Path]:
    return [p for p in packets.iterdir() if p.is_dir()]


# --------------------------------------------------------------------------- the daemon fails after the render


def test_a_503_on_the_first_hash_after_the_render_removes_the_packet_folder(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    daemon.hasher.override = (503, {"error": "daemon restarting"})  # every /hash: past the 3 retries
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "legend"
    assert len(daemon.hasher.requests) == 4  # the first call and T142's three retries
    assert len(scene.render_calls) == 1  # it did render: the folder existed until the failure
    assert packet_folders(packets) == [] and result.packet_dir is None and result.packet_id
    assert result.errors[0].startswith("legend: daemon: ") and "503" in result.errors[0]
    [warning] = [w for w in result.warnings if "was removed" in w]  # the lint note stays
    assert str(packets) in warning and result.packet_id in warning
    assert "the daemon failed at legend" in warning
    assert "503" in warning and "re-send when it is back" in warning and "keep_failed_packets" in warning
    assert "daemon: " not in warning  # the stage prefix is not repeated inside the parentheses
    assert daemon.posted == []


def test_keep_failed_packets_keeps_the_render_of_a_daemon_failure(install_scene, daemon_env):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    daemon.hasher.override = (503, {"error": "daemon restarting"})
    [result] = send(["CAM_front"], packets, cfg, keep_failed_packets=True)
    assert not result.ok and result.stage == "legend" and result.packet_dir
    root = Path(result.packet_dir)
    assert (root / "beauty.exr").is_file() and (root / "display.tif").is_file()
    assert not (root / "manifest.json").exists()
    assert not any("was removed" in w for w in result.warnings)
    assert any("kept" in w and "no manifest" in w for w in result.warnings)


@pytest.mark.parametrize("status", [403, 404, 422])
def test_a_daemon_that_refuses_keeps_the_render_for_inspection(install_scene, daemon_env, status):
    """403/404/4xx on /hash is an answer, not a failure: the same one comes again until the config or the
    packets folder changes, so the render stays (test_stress_wave12_send pins the 403 at the hash stage)."""
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    daemon.hasher.override = (status, {"error": "refused"})
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "legend" and len(daemon.hasher.requests) == 1  # not retried
    [folder] = packet_folders(packets)
    assert Path(result.packet_dir) == folder and (folder / "beauty.exr").is_file()
    assert not any("was removed" in w for w in result.warnings)
    assert any("kept" in w and "no manifest" in w for w in result.warnings)


def test_daemon_failed_tells_a_failure_from_a_refusal():
    failed = send_packet.daemon_failed
    assert failed(transport.DaemonUnreachable("x")) and failed(transport.DaemonTimeout("x"))
    assert failed(transport.UnexpectedStatus(503, "restarting")) and failed(
        transport.UnexpectedStatus(500, "")
    )
    assert failed(transport.UnexpectedStatus(200, "not json")) and failed(transport.PostTimedOut(None))
    assert failed(transport.TransportError("an answer that is not one"))
    assert not failed(transport.Unauthorized("token")) and not failed(transport.HashRefused(403, "outside"))
    assert not failed(transport.HashRefused(None, "no hash")) and not failed(
        transport.UnexpectedStatus(422, "")
    )


@pytest.mark.parametrize("failure", [transport.DaemonUnreachable, transport.DaemonTimeout])
def test_a_dropped_connection_or_a_timeout_behaves_the_same(install_scene, daemon_env, monkeypatch, failure):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    real = transport._request

    def drop(req, timeout_s):
        if req.full_url.endswith("/api/v0/hash"):
            raise failure(f"{req.full_url}: connection dropped")
        return real(req, timeout_s)

    monkeypatch.setattr(transport, "_request", drop)
    assert issubclass(failure, transport.TransportError)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "legend" and len(scene.render_calls) == 1
    assert packet_folders(packets) == [] and result.packet_dir is None
    assert any("was removed" in w and "the daemon failed at legend" in w for w in result.warnings)
    assert "connection dropped" in " ".join(result.warnings)


def test_a_daemon_failure_after_the_legend_stage_removes_the_folder_too(install_scene, daemon_env):
    """The legend digests are hashed first; the packet's own files are hashed after (stage hash)."""
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    real = daemon.hasher.answer
    seen = {"n": 0}

    def later_calls_fail(headers, body):
        seen["n"] += 1
        if seen["n"] == 1:
            return real(headers, body)
        daemon.hasher.requests.append(body)
        return 503, {"error": "daemon went away"}

    daemon.hasher.answer = later_calls_fail
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage != "legend"
    assert packet_folders(packets) == [] and result.packet_dir is None
    assert any("was removed" in w and f"the daemon failed at {result.stage}" in w for w in result.warnings)


def test_a_post_that_fails_after_the_manifest_keeps_the_folder_the_daemon_may_ingest(
    install_scene, daemon_env, monkeypatch
):
    daemon, cfg, packets = daemon_env
    vray_scene(install_scene)
    real = transport._request

    def drop(req, timeout_s):
        if req.full_url.endswith("/api/v0/packets"):
            raise transport.DaemonUnreachable("connection dropped")
        return real(req, timeout_s)

    monkeypatch.setattr(transport, "_request", drop)
    [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "post"
    [folder] = packet_folders(packets)
    assert (folder / "manifest.json").is_file()  # a manifest: the settle step never ran
    assert not any("was removed" in w for w in result.warnings)


def test_the_unlabelled_share_abort_still_keeps_its_render(install_scene, daemon_env):
    """A scene problem found after the render is not the daemon's: the folder stays for inspection."""
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
    [folder] = packet_folders(packets)
    assert Path(result.packet_dir) == folder and (folder / "beauty.exr").is_file()
    assert not (folder / "manifest.json").exists()
    assert not any("was removed" in w for w in result.warnings)
    assert any("no manifest" in w and "kept" in w for w in result.warnings)


def test_settle_failed_folder_reports_the_error_it_was_given(tmp_path):
    folder = tmp_path / "pkt_x"
    folder.mkdir()
    (folder / "beauty.exr").write_bytes(b"x")
    result = send_packet.SendResult(camera="CAM_a", stage="hash", packet_dir=str(folder))
    result.errors.append("hash: daemon: http://127.0.0.1:4610/api/v0/hash: connection refused")
    send_packet.settle_failed_folder(result, None, {}, transport_failure=True)
    assert not folder.exists() and result.packet_dir is None
    assert result.warnings == [
        f"the packet folder {folder} was removed: the daemon failed at hash "
        "(http://127.0.0.1:4610/api/v0/hash: connection refused) after the render; re-send when it is "
        "back (set keep_failed_packets to keep failed renders)"
    ]
    # without the flag a SendAbort at "legend" is a scene problem: the folder stays
    keep = tmp_path / "pkt_y"
    keep.mkdir()
    result = send_packet.SendResult(camera="CAM_a", stage="legend", packet_dir=str(keep))
    send_packet.settle_failed_folder(result, "legend", {})
    assert keep.is_dir() and result.packet_dir == str(keep)
    assert any("kept" in w for w in result.warnings)


# --------------------------------------------------------------------------- long scene names


def save_scene(runtime, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    runtime.maxFilePath, runtime.maxFileName = str(folder), name
    return folder / name


@pytest.mark.parametrize("length", [249, 254])
def test_a_long_stem_keeps_its_sidecar_under_app_data(scene, tmp_path, length):
    max_file = save_scene(scene, tmp_path / "jobs", "a" * length + ".max")
    path = recipes.sidecar_path(max_file)
    assert path.parent == recipes.SIDECAR_FALLBACK_DIR == tmp_path / "plate-home" / "sidecars"
    key = hashlib.blake2b(str(max_file.resolve()).encode("utf-8"), digest_size=8).hexdigest()
    assert path.name == f"{key}.plate.json" and len(key) == 16
    assert recipes.sidecar_note(max_file) == (
        f"sidecar stored under {recipes.SIDECAR_FALLBACK_DIR} (the scene name is too long for one beside it)"
    )
    live = ctl.LiveController()
    assert live.binding_problem() is None and live.project_id() == "a" * 64  # the stem slug, unbound
    saved = live.set_project("clubhouse")
    assert saved == path and path.is_file()
    assert ctl.scene_project_id(max_file) == "clubhouse"
    assert ctl.LiveController().project_id() == "clubhouse"  # a new session reads it back
    recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a", preset="night-relight")}, max_file)
    assert recipes.load_recipes(max_file)["CAM_a"].preset == "night-relight"
    assert recipes.get_recipe("CAM_a", max_file).preset == "night-relight"
    assert recipes.set_recipe(CameraRecipe(camera="CAM_b", engine_id="flux-3"), max_file) == path
    assert sorted(recipes.load_recipes(max_file)) == ["CAM_a", "CAM_b"]
    assert ctl.scene_project_id(max_file) == "clubhouse"  # saving recipes kept the binding
    assert list((tmp_path / "jobs").iterdir()) == []  # nothing beside the scene
    assert [p.name for p in recipes.SIDECAR_FALLBACK_DIR.iterdir()] == [path.name]


def test_two_long_scenes_never_share_a_sidecar(scene, tmp_path):
    first = save_scene(scene, tmp_path / "one", "a" * 250 + ".max")
    second = save_scene(scene, tmp_path / "two", "a" * 250 + ".max")  # same name, another folder
    third = save_scene(scene, tmp_path / "one", "a" * 249 + "b.max")
    assert len({recipes.sidecar_path(p) for p in (first, second, third)}) == 3


def test_load_save_default_to_the_open_scene(scene, tmp_path):
    save_scene(scene, tmp_path / "jobs", "a" * 254 + ".max")
    saved = recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a", preset="x")})
    assert saved.parent == recipes.SIDECAR_FALLBACK_DIR
    assert recipes.load_recipes()["CAM_a"].preset == "x"


def test_a_200_character_stem_stays_beside_the_scene(scene, tmp_path):
    max_file = save_scene(scene, tmp_path / "jobs", "a" * 200 + ".max")
    path = recipes.sidecar_path(max_file)
    assert path == tmp_path / "jobs" / ("a" * 200 + ".plate.json")
    assert recipes.sidecar_note(max_file) is None
    assert ctl.LiveController().set_project("clubhouse") == path and path.is_file()
    assert not recipes.SIDECAR_FALLBACK_DIR.exists()


def test_the_name_limit_is_255_utf8_bytes_not_characters(scene, tmp_path):
    suffix = len(recipes.SIDECAR_SUFFIX)
    fits = tmp_path / ("a" * (255 - suffix) + ".max")
    over = tmp_path / ("a" * (255 - suffix + 1) + ".max")
    assert recipes.sidecar_path(fits) == tmp_path / ("a" * (255 - suffix) + ".plate.json")
    assert recipes.sidecar_path(over).parent == recipes.SIDECAR_FALLBACK_DIR
    wide = tmp_path / ("é" * 125 + ".max")  # 125 characters, 250 bytes, 261 with the suffix
    assert len(wide.stem) == 125 and recipes.sidecar_path(wide).parent == recipes.SIDECAR_FALLBACK_DIR
    assert recipes.fits_filesystem(tmp_path / ("é" * 127)) and not recipes.fits_filesystem(
        tmp_path / ("é" * 128)
    )


def test_an_unsaved_scene_still_has_no_sidecar(scene):
    assert recipes.sidecar_path(None) is None and recipes.sidecar_path("") is None
    assert recipes.sidecar_note(None) is None


def virtual_path(length: int, name: str) -> Path:
    """A POSIX path of exactly `length` characters ending in `name`, under made-up folders (nothing is
    created: sidecar_path and fits_filesystem only look at the text)."""
    head = Path("/scenes") / ("d" * 100)
    middle = length - len(str(head)) - 1 - len(name) - 1  # the "/" before the middle folder and the name
    assert 1 <= middle <= 255
    path = head / ("e" * middle) / name
    assert len(str(path)) == length
    return path


def test_windows_adds_the_259_character_path_limit(monkeypatch, tmp_path):
    assert recipes.WINDOWS_MAX_PATH == 259
    exact = virtual_path(259, "Clubhouse.plate.json")
    over = virtual_path(260, "Clubhouse.plate.json")
    assert recipes.fits_filesystem(over) and recipes.fits_filesystem(exact)  # not Windows: only the name
    monkeypatch.setattr(recipes, "platform_is_windows", lambda: True)
    assert recipes.fits_filesystem(exact) and not recipes.fits_filesystem(over)
    # the sidecar of a scene whose natural sidecar path is 259 characters stays beside it, 260 moves
    stays = virtual_path(259, "Clubhouse.plate.json").with_name("Clubhouse.max")
    moves = virtual_path(260, "Clubhouse.plate.json").with_name("Clubhouse.max")
    assert recipes.sidecar_path(stays) == stays.with_name("Clubhouse.plate.json")
    assert recipes.sidecar_note(stays) is None
    assert recipes.sidecar_path(moves).parent == recipes.SIDECAR_FALLBACK_DIR
    assert "too long" in recipes.sidecar_note(moves)
    # a 230-character .max path is fine for the sidecar (237 characters)
    scene = virtual_path(230, "Clubhouse.max")
    assert recipes.sidecar_path(scene) == scene.with_name("Clubhouse.plate.json")


def test_windows_path_limit_moves_the_backup_before_the_sidecar(scene, tmp_path, monkeypatch):
    monkeypatch.setattr(recipes, "platform_is_windows", lambda: True)
    folder = tmp_path / "jobs"
    folder.mkdir()
    stamp = "20261019-120000"
    suffix = len(safety.BACKUP_INFIX) + len(stamp) + len(".max")

    def scene_whose_backup_is(total: int) -> Path:
        stem = "s" * (total - suffix - len(str(folder)) - 1)
        return folder / f"{stem}.max"

    fits = scene_whose_backup_is(259)
    assert safety.backup_path_for(fits, stamp).parent == folder and safety.backup_note(fits, "x") is None
    over = scene_whose_backup_is(260)
    dest = safety.backup_path_for(over, stamp)
    assert dest.parent == Path(tempfile.gettempdir()) and dest.name.startswith("plate-")
    assert recipes.sidecar_path(over) == over.with_name(over.stem + ".plate.json")  # 238 characters: fine


def test_platform_is_windows_follows_os_name(monkeypatch):
    import os

    assert recipes.platform_is_windows() is (os.name == "nt")


def test_fallback_dir_follows_plate_home_each_time(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "one"))
    assert recipes.SIDECAR_FALLBACK_DIR == tmp_path / "one" / "sidecars"
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "two"))
    assert recipes.SIDECAR_FALLBACK_DIR == tmp_path / "two" / "sidecars"
    with pytest.raises(AttributeError):
        recipes.NO_SUCH_NAME  # noqa: B018


def test_a_fallback_folder_that_cannot_be_written_is_still_a_message(scene, tmp_path):
    max_file = save_scene(scene, tmp_path / "jobs", "a" * 254 + ".max")
    home = tmp_path / "plate-home"
    home.mkdir()
    (home / "sidecars").write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(SidecarUnwritable) as caught:
        ctl.LiveController().set_project("clubhouse")
    assert not isinstance(caught.value, OSError) and "cannot be written" in str(caught.value)
    with pytest.raises(SidecarUnwritable):
        recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a")}, max_file)


def test_the_backup_of_a_long_scene_lands_in_the_temp_folder(scene, tmp_path):
    max_file = save_scene(scene, tmp_path / "jobs", "a" * 254 + ".max")
    stamp = "20261019-120000"
    dest = safety.backup_path_for(max_file, stamp)
    key = recipes.scene_key(max_file)
    assert dest == Path(tempfile.gettempdir()) / f"plate-{key}.plate-backup.{stamp}.max"
    assert recipes.sidecar_path(max_file).name == f"{key}.plate.json"  # the same hash, by design
    assert ":" not in dest.name and len(dest.name.encode("utf-8")) <= 255
    note = safety.backup_note(max_file, dest)
    assert (
        note
        == f"backup stored in {dest.parent} as {dest.name} (the scene name is too long for one beside it)"
    )
    # a 200-character stem has room for both: next to the scene, no note
    short = save_scene(scene, tmp_path / "jobs", "a" * 200 + ".max")
    beside = safety.backup_path_for(short, stamp)
    assert beside == tmp_path / "jobs" / ("a" * 200 + f".plate-backup.{stamp}.max")
    assert safety.backup_note(short, beside) is None


def test_the_backup_can_overflow_before_the_sidecar_does(scene, tmp_path):
    # 244 + len(".plate.json") = 255 fits; 244 + the backup's suffix does not
    max_file = save_scene(scene, tmp_path / "jobs", "a" * 244 + ".max")
    assert recipes.sidecar_path(max_file) == tmp_path / "jobs" / ("a" * 244 + ".plate.json")
    dest = safety.backup_path_for(max_file, "20261019-120000")
    assert dest.parent == Path(tempfile.gettempdir()) and dest.name.startswith("plate-")


def test_an_unsaved_scene_backup_is_unchanged(scene):
    dest = safety.backup_path_for(None, "20261019-120000")
    assert dest == Path(tempfile.gettempdir()) / "untitled.plate-backup.20261019-120000.max"
    assert safety.backup_note(None, dest) is None


def test_send_proceeds_for_a_254_character_scene_and_logs_where_the_backup_went(
    install_scene, daemon_env, tmp_path
):
    daemon, cfg, packets = daemon_env
    scene, _ = vray_scene(install_scene)
    max_file = save_scene(scene, tmp_path / "jobs", "a" * 254 + ".max")
    [result] = send(["CAM_front"], packets, cfg, backup=True, project_id=None)
    assert result.ok, (result.stage, result.errors)
    assert Path(result.backup).parent == Path(tempfile.gettempdir())
    assert Path(result.backup).is_file() and Path(result.backup).name.startswith("plate-")
    assert any(
        "backup stored in" in w and "too long" in w and Path(result.backup).name in w for w in result.warnings
    )
    assert scene.save_calls[0]["useNewFile"] is False
    assert recipes.sidecar_path(max_file).parent == recipes.SIDECAR_FALLBACK_DIR
    assert len(packet_folders(packets)) == 1
