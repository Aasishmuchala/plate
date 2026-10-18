from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from plate_max import config

# Tokens are 16-256 printable ASCII characters (config.token_problem); these read as their role.
FROM_CONFIG = "from-config-0123456789"
FROM_FILE = "from-file-0123456789"
STORED = "stored-0123456789abcdef"


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
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"token": STORED}).encode("utf-8"))
    assert config.load_config()["token"] == STORED
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
        assert folder.as_posix().endswith("/Plate")
    assert config.config_path() == folder / "config.json"


def test_default_folder_is_what_the_daemon_gets_from_platformdirs(monkeypatch):
    platformdirs = pytest.importorskip("platformdirs")
    monkeypatch.delenv("PLATE_HOME", raising=False)
    assert config.app_data_dir() == Path(platformdirs.user_data_dir("Plate", appauthor=False))


def test_linux_folder_honours_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PLATE_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.app_data_dir() == tmp_path / "xdg" / "Plate"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert config.app_data_dir() == Path.home() / ".local" / "share" / "Plate"


def test_empty_environment_variables_count_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", "")
    monkeypatch.setenv("PLATE_CONFIG", "")
    monkeypatch.setenv("PLATE_TOKEN_FILE", "")
    assert config.app_data_dir().name == "Plate"
    assert config.config_path() == config.app_data_dir() / "config.json"
    assert config.token_file_path() is None and config.read_token_file() is None


def test_plate_config_wins_over_plate_home_for_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "elsewhere" / "c.json"))
    assert config.app_data_dir() == tmp_path / "home"
    assert config.config_path() == tmp_path / "elsewhere" / "c.json"
    assert config.packets_dir({}) == tmp_path / "home" / "packets"


# ---- one config.json


def test_one_config_json_holds_the_four_documented_keys(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    assert config.CONFIG_KEYS == ("token", "daemon_url", "repo_path", "packets_dir")
    cfg = {
        "token": "ab" * 16,
        "daemon_url": "http://127.0.0.1:4610",
        "repo_path": "D:\\plate",
        "packets_dir": "\\\\nas\\plate\\packets",
    }
    config.save_config(cfg)
    assert config.load_config() == cfg
    assert json.loads(path.read_text(encoding="utf-8")) == cfg
    assert config.packets_dir() == Path(cfg["packets_dir"])


def test_a_partial_file_keeps_the_defaults_for_the_rest(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    path.write_text(json.dumps({"repo_path": "D:\\plate"}), encoding="utf-8", newline="\n")
    cfg = config.load_config()
    assert cfg == {"token": "", "daemon_url": config.DEFAULT_DAEMON_URL, "repo_path": "D:\\plate"}


def test_a_config_that_is_not_an_object_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    path.write_text("[1, 2]", encoding="utf-8")
    assert config.load_config() == {"token": "", "daemon_url": config.DEFAULT_DAEMON_URL}


# ---- PLATE_TOKEN_FILE


def _token_file(tmp_path, monkeypatch, content: bytes, name="token.txt"):
    path = tmp_path / name
    path.write_bytes(content)
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(path))
    return path


def test_token_file_wins_over_config_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "config.json"))
    config.save_config({"token": FROM_CONFIG, "daemon_url": "http://127.0.0.1:4610"})
    assert config.load_config()["token"] == FROM_CONFIG
    _token_file(tmp_path, monkeypatch, FROM_FILE.encode("ascii") + b"\n")
    cfg = config.load_config()
    assert cfg["token"] == FROM_FILE and cfg["daemon_url"] == "http://127.0.0.1:4610"


def test_token_file_works_with_no_config_json_at_all(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch, b"only-the-file-0123456789")
    assert config.load_config() == {
        "token": "only-the-file-0123456789",
        "daemon_url": config.DEFAULT_DAEMON_URL,
    }


def test_token_file_is_stripped_and_bom_tolerant(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch, b"\xef\xbb\xbf  abc123abc123abc123 \r\n")
    assert config.read_token_file() == "abc123abc123abc123"


@pytest.mark.parametrize("content", [b"", b"   \r\n", None])
def test_an_empty_or_missing_token_file_falls_back_to_config_json(tmp_path, monkeypatch, content):
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "config.json"))
    config.save_config({"token": FROM_CONFIG})
    if content is None:
        monkeypatch.setenv("PLATE_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    else:
        _token_file(tmp_path, monkeypatch, content)
    assert config.read_token_file() is None
    assert config.load_config()["token"] == FROM_CONFIG


def test_saving_never_writes_the_token_file_secret_into_config_json(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    config.save_config({"token": STORED, "daemon_url": "http://127.0.0.1:4610"})
    _token_file(tmp_path, monkeypatch, FROM_FILE.encode("ascii"))
    cfg = config.load_config()
    assert cfg["token"] == FROM_FILE
    cfg["repo_path"] = "D:\\plate"
    config.save_config(cfg)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["token"] == STORED and on_disk["repo_path"] == "D:\\plate"
    assert FROM_FILE not in path.read_text(encoding="utf-8")


def test_saving_with_no_stored_token_leaves_the_key_out(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    _token_file(tmp_path, monkeypatch, FROM_FILE.encode("ascii"))
    config.save_config(config.load_config())
    assert "token" not in json.loads(path.read_text(encoding="utf-8"))


def test_an_explicitly_different_token_is_saved_as_given(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    _token_file(tmp_path, monkeypatch, FROM_FILE.encode("ascii"))
    config.save_config({"token": "installer-wrote-this"})
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "installer-wrote-this"


# ---- the token is checked where it is read


@pytest.mark.parametrize(
    ("token", "why"),
    [
        ("short", "5 characters long"),
        ("x" * 257, "257 characters long"),
        ("fedcba9876543210fedcba9876543210\n# rotated 2026-10-01", "whitespace"),
        ("fedcba9876543210 fedcba9876543210", "whitespace"),
        ("fedcba9876543210\tfedcba", "whitespace"),
        ("fedcba9876543210fedcba98765432\u00e9", "not printable ASCII"),
        ("fedcba9876543210\ufefffedcba9876543210", "not printable ASCII"),
        ("fedcba9876543210\x7ffedcba9876543210", "not printable ASCII"),
        (1234567890123456789, "not text"),
    ],
)
def test_token_problem_names_why_without_the_token(token, why):
    problem = config.token_problem(token)
    assert problem is not None and why in problem
    assert str(token) not in problem


def test_good_tokens_pass():
    for token in ("a" * 16, "x" * 256, "Zx-_.~!0123456789abcdef", "0123456789abcdef0123456789abcdef"):
        assert config.token_problem(token) is None


def test_a_token_file_with_a_rotated_line_gives_no_token_and_says_so_once(tmp_path, monkeypatch, caplog):
    secret = "fedcba9876543210fedcba9876543210"
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "config.json"))
    config.save_config({"token": FROM_CONFIG, "daemon_url": "http://127.0.0.1:4610"})
    _token_file(tmp_path, monkeypatch, (secret + "\n# rotated 2026-10-01\n").encode("ascii"))
    config._reported.clear()
    with caplog.at_level("WARNING", logger="plate_max.config"):
        assert config.read_token_file() is None
        cfg = config.load_config()
        config.load_config()  # the poller reads it every tick: logged once
    assert cfg["token"] is None  # never the stale config.json token
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "token source: PLATE_TOKEN_FILE: the token is not used because it has whitespace in it "
        "(a space, a tab or a second line)"
    ]
    assert secret not in caplog.text and FROM_CONFIG not in caplog.text


def test_a_malformed_config_json_token_gives_no_token(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    bad = "fedcba9876543210fedcba9876543210\r\n"
    path.write_text(
        json.dumps({"token": bad, "daemon_url": "http://127.0.0.1:4610"}), encoding="utf-8", newline="\n"
    )
    config._reported.clear()
    with caplog.at_level("WARNING", logger="plate_max.config"):
        cfg = config.load_config()
    assert cfg["token"] is None and cfg["daemon_url"] == "http://127.0.0.1:4610"
    assert "token source: config.json:" in caplog.text and bad.strip() not in caplog.text
    path.write_text(json.dumps({"token": None}), encoding="utf-8", newline="\n")
    assert config.load_config()["token"] == ""  # null is no token, not a refused one


def test_a_good_token_file_still_wins_over_a_malformed_config_json_token(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    path.write_text(json.dumps({"token": "short"}), encoding="utf-8", newline="\n")
    _token_file(tmp_path, monkeypatch, FROM_FILE.encode("ascii"))
    assert config.load_config()["token"] == FROM_FILE


def test_saving_never_writes_a_refused_token_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLATE_CONFIG", str(path))
    bad = "fedcba9876543210 with a space"
    path.write_text(
        json.dumps({"token": bad, "daemon_url": "http://127.0.0.1:4610"}), encoding="utf-8", newline="\n"
    )
    cfg = config.load_config()
    assert cfg["token"] is None
    cfg["repo_path"] = "D:\\plate"
    config.save_config(cfg)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["token"] == bad and on_disk["repo_path"] == "D:\\plate"  # left for the artist to fix
    path.unlink()
    _token_file(tmp_path, monkeypatch, b"two\nlines-of-token-here")
    config.save_config(config.load_config())
    assert "token" not in json.loads(path.read_text(encoding="utf-8"))
