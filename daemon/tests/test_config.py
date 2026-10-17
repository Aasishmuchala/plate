"""config.py: app-data paths, config.json creation, the token and its override, allowlists, bind rules."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import platformdirs
import pytest

from plate_daemon import config, paths, settings
from plate_daemon.config import ConfigError, Paths, Settings


def test_home_defaults_to_platformdirs_without_plate_home(monkeypatch):
    monkeypatch.delenv("PLATE_HOME")
    assert Paths.from_env().home == Path(platformdirs.user_data_dir("Plate", appauthor=False))


def test_paths_follow_plate_home_and_plate_config(tmp_path, monkeypatch):
    p = Paths.from_env()
    assert p.home == tmp_path
    assert p.config_path == tmp_path / "config.json"
    assert p.index_db("tula-wot") == tmp_path / "index" / "tula-wot.sqlite"
    assert (p.logs_dir, p.cache_dir, p.packets_dir) == (
        tmp_path / "logs",
        tmp_path / "cache",
        tmp_path / "packets",
    )
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "elsewhere" / "cfg.json"))
    assert Paths.from_env().config_path == tmp_path / "elsewhere" / "cfg.json"


def test_ensure_config_writes_an_owner_only_urlsafe_token(tmp_path):
    cfg = Paths.from_env().ensure_config()
    path = tmp_path / "config.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == cfg
    assert cfg["daemon_url"] == "http://127.0.0.1:4610"
    assert len(cfg["token"]) >= 43 and set(cfg["token"]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A second call keeps the token; plugin-owned keys survive a token being added.
    assert Paths.from_env().ensure_config()["token"] == cfg["token"]
    path.write_text(json.dumps({"packets_dir": "D:\\PLATE\\packets"}), encoding="utf-8", newline="\n")
    again = Paths.from_env().ensure_config()
    assert again["packets_dir"] == "D:\\PLATE\\packets" and again["token"]


def test_token_file_wins_over_config_json(tmp_path, monkeypatch):
    Paths.from_env().ensure_config()
    tf = tmp_path / "token.txt"
    tf.write_text("\ufefffrom-the-token-file-0123456789\r\n", encoding="utf-8")
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(tf))
    assert Paths.from_env().token() == "from-the-token-file-0123456789"
    assert Settings.load().token == "from-the-token-file-0123456789"


def test_a_missing_or_short_token_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(tmp_path / "absent.txt"))
    with pytest.raises(ConfigError):
        Settings.load()
    short = tmp_path / "short.txt"
    short.write_text("abc", encoding="utf-8")
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(short))
    with pytest.raises(ConfigError, match="at least"):
        Settings.load()


def test_allowlists_default_and_dev(monkeypatch):
    s = Settings.load()
    assert s.host_allowlist == {"127.0.0.1:4610", "localhost:4610"}
    assert s.origin_allowlist == {"http://127.0.0.1:4610", "http://localhost:4610"}
    assert s.bind == ["127.0.0.1"] and not s.dev
    monkeypatch.setenv("PLATE_DEV", "1")
    d = Settings.load()
    assert d.dev
    assert {"http://127.0.0.1:5173", "http://localhost:5173"} <= d.origin_allowlist
    assert d.host_allowlist == s.host_allowlist
    monkeypatch.setenv("PLATE_DEV", "0")
    assert not Settings.load().dev


def test_tailscale_is_a_second_bind_and_allowlisted(monkeypatch):
    monkeypatch.setenv("PLATE_BIND", "127.0.0.1,100.101.102.103")
    s = Settings.load()
    assert s.bind_host == "127.0.0.1" and s.tailscale_ip == "100.101.102.103"
    assert s.bind == ["127.0.0.1", "100.101.102.103"]
    assert "100.101.102.103:4610" in s.host_allowlist
    assert "http://100.101.102.103:4610" in s.origin_allowlist
    monkeypatch.delenv("PLATE_BIND")
    monkeypatch.setenv("PLATE_TAILSCALE_IP", "100.64.0.9")
    assert Settings.load().bind == ["127.0.0.1", "100.64.0.9"]


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "127.0.0.1,0.0.0.0", "127.0.0.1,100.64.0.1,100.64.0.2"])
def test_bad_binds_are_refused(monkeypatch, bind):
    monkeypatch.setenv("PLATE_BIND", bind)
    with pytest.raises(ConfigError):
        Settings.load()


def test_port_changes_the_allowlist(monkeypatch):
    monkeypatch.setenv("PLATE_PORT", "4700")
    s = Settings.load()
    assert s.host_allowlist == {"127.0.0.1:4700", "localhost:4700"}
    assert s.ui_url.startswith("http://127.0.0.1:4700/#token=")


def test_packets_dirs_include_config_packets_dir(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"packets_dir": str(tmp_path / "nas" / "packets")}), encoding="utf-8", newline="\n"
    )
    s = Settings.load()
    assert s.packets_dirs == [tmp_path / "packets", tmp_path / "nas" / "packets"]
    assert s.projects_dir == tmp_path / "projects"


def test_retired_modules_reexport_config():
    assert settings.Settings is config.Settings and settings.ConfigError is config.ConfigError
    assert paths.check_project_id is config.check_project_id and paths.ensure_local is config.ensure_local
    assert not hasattr(config, "cas_root") and not hasattr(paths, "cas_root")
