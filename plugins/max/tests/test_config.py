from __future__ import annotations

import json
import sys

from plate_max import config


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    assert config.load_config() == {"token": "", "daemon_url": config.DEFAULT_DAEMON_URL}
    saved = config.save_config(
        {"token": "ab" * 16, "daemon_url": "http://127.0.0.1:4610", "project_id": "tula-wot"}
    )
    assert saved == path
    assert config.load_config()["project_id"] == "tula-wot"
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8"))["token"] == "ab" * 16


def test_load_tolerates_a_powershell_bom_and_garbage(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"token": "t"}).encode("utf-8"))
    assert config.load_config()["token"] == "t"
    path.write_text("{not json", encoding="utf-8")
    assert config.load_config()["token"] == ""


def test_plate_home_without_plate_config(tmp_path, monkeypatch):
    monkeypatch.delenv("PLATE_CONFIG", raising=False)
    monkeypatch.setenv("PLATE_HOME", str(tmp_path))
    assert config.app_data_dir() == tmp_path
    assert config.config_path() == tmp_path / "config.json"
    assert config.packets_dir({}) == tmp_path / "packets"
    assert config.packets_dir({"packets_dir": str(tmp_path / "nas")}) == tmp_path / "nas"


def test_default_app_data_dir_per_platform(monkeypatch):
    monkeypatch.delenv("PLATE_CONFIG", raising=False)
    monkeypatch.delenv("PLATE_HOME", raising=False)
    folder = config.app_data_dir()
    assert folder.name == "Plate"
    if sys.platform == "win32":
        import os

        assert str(folder).startswith(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        assert folder.as_posix().endswith("Library/Application Support/Plate")
    else:
        assert folder.as_posix().endswith(".local/share/Plate")
    assert config.config_path() == folder / "config.json"
