"""Verdict labels live in the data folder, not the repo (router plan T110).

The default labels file is `<data home>/calib/labels.jsonl`; the override order stays settings.labels_path,
then PLATE_LABELS, then that default. The labelling tool resolves the same file. A repo-local
`calib/labels.jsonl` left by an older build is read once with a printed warning and never written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plate_daemon import verdicts
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine

REPO = verdicts.REPO
REAL_REPO_LABELS = REPO / "calib" / "labels.jsonl"
ROOM_A = REPO / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id


@pytest.fixture(autouse=True)
def isolated_labels_env(tmp_path_factory, monkeypatch):
    """No PLATE_LABELS, and the legacy file pointed at a folder that does not exist (a dev checkout may have
    a real one); the once-per-process cache starts empty."""
    monkeypatch.delenv("PLATE_LABELS", raising=False)
    monkeypatch.setattr(
        verdicts, "LEGACY_LABELS", tmp_path_factory.mktemp("oldrepo") / "calib" / "labels.jsonl"
    )
    monkeypatch.setattr(verdicts, "_LEGACY_SEEN", {})


def _label(run_id: str = "r1") -> dict:
    return verdicts.build_label(
        at=datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC),
        rater="aasish",
        run_id=run_id,
        verdict="fix",
        reasons=["P1"],
        candidate="ab" * 32,
    )


# ---------------------------------------------------------------------------- the default path


def test_default_is_under_the_settings_home(settings, plate_env):
    expected = plate_env / "calib" / "labels.jsonl"
    assert verdicts.labels_path(settings) == expected
    assert settings.home == plate_env  # PLATE_HOME is the data folder
    assert verdicts.default_labels_path(settings) == expected


def test_default_without_settings_follows_plate_home(plate_env):
    assert verdicts.labels_path() == plate_env / "calib" / "labels.jsonl"


def test_settings_home_wins_over_the_environment(tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("other-home")
    monkeypatch.setenv("PLATE_HOME", str(tmp_path_factory.mktemp("env-home")))
    s = Settings.load(env={"PLATE_HOME": str(other)})
    assert verdicts.labels_path(s) == other / "calib" / "labels.jsonl"


def test_default_without_plate_home_is_the_os_data_dir(tmp_path, monkeypatch):
    import platformdirs

    calls = []

    def fake_user_data_dir(*args, **kwargs):
        calls.append((args, kwargs))
        return str(tmp_path / "appdata" / "Plate")

    monkeypatch.delenv("PLATE_HOME", raising=False)
    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)
    assert verdicts.labels_path() == tmp_path / "appdata" / "Plate" / "calib" / "labels.jsonl"
    assert calls == [(("Plate",), {"appauthor": False})]


def test_default_is_never_inside_the_repo(settings, monkeypatch):
    assert REPO not in verdicts.labels_path(settings).parents
    monkeypatch.delenv("PLATE_HOME", raising=False)
    assert REPO not in verdicts.labels_path().parents  # the real OS data dir


def test_override_order_is_settings_then_env_then_default(settings, tmp_path, monkeypatch):
    default = settings.home / "calib" / "labels.jsonl"
    env_file = tmp_path / "env" / "labels.jsonl"
    declared = tmp_path / "declared.jsonl"
    assert verdicts.labels_path(settings) == default
    monkeypatch.setenv("PLATE_LABELS", str(env_file))
    assert verdicts.labels_path(settings) == env_file
    assert verdicts.labels_path() == env_file
    with_path = SimpleNamespace(home=settings.home, labels_path=declared)
    assert verdicts.labels_path(with_path) == declared
    monkeypatch.delenv("PLATE_LABELS")
    assert verdicts.labels_path(with_path) == declared
    assert verdicts.labels_path(SimpleNamespace(home=settings.home, labels_path=None)) == default
    assert verdicts.labels_path(SimpleNamespace(home=settings.home)) == default


def test_append_creates_the_data_folder_and_writes_lf_utf8(settings):
    path = verdicts.labels_path(settings)
    assert not path.parent.exists()
    verdicts.append_label(path, _label("r1"))
    verdicts.append_label(path, _label("r2"))
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    assert [json.loads(x)["run_id"] for x in raw.decode("utf-8").splitlines()] == ["r1", "r2"]


def test_the_labeller_resolves_the_same_file(settings, tmp_path, monkeypatch):
    store = pytest.importorskip("plate_label.store")
    assert store.default_labels_path() == verdicts.labels_path(settings)
    assert store.default_labels_path() == verdicts.labels_path()
    monkeypatch.setenv("PLATE_LABELS", str(tmp_path / "elsewhere.jsonl"))
    assert store.default_labels_path() == verdicts.labels_path(settings)
    monkeypatch.delenv("PLATE_LABELS")
    monkeypatch.delenv("PLATE_HOME")
    assert store.default_labels_path() == verdicts.labels_path()  # both on the OS data dir
    # a label the daemon wrote is a line the labeller reads, and the other way round
    path = tmp_path / "shared.jsonl"
    verdicts.append_label(path, _label("r9"))
    assert [r["run_id"] for r in store.read_labels(path)] == ["r9"]


# ---------------------------------------------------------------------------- the legacy repo-local file


def _write_legacy(lines: list[str], tail: str = "") -> Path:
    legacy = Path(verdicts.LEGACY_LABELS)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(("\n".join(lines) + "\n" + tail).encode("utf-8"))
    return legacy


def test_a_legacy_file_is_read_once_with_one_warning_and_never_written(settings, capsys):
    old = [
        json.dumps(_label("old1"), separators=(",", ":")),
        json.dumps(_label("old2"), separators=(",", ":")),
    ]
    legacy = _write_legacy(old, tail='{"v":1,"torn')
    before = legacy.read_bytes()
    target = verdicts.labels_path(settings)  # the first resolution warns
    first = capsys.readouterr().err
    assert first.count("older build") == 1
    assert (
        str(legacy) in first and str(target) in first and "never written" in first and "2 label(s)" in first
    )

    records = verdicts.legacy_labels(target)
    assert [r["run_id"] for r in records] == ["old1", "old2"]  # the torn tail is not a label
    for _ in range(3):
        verdicts.labels_path(settings)
    assert capsys.readouterr().err == ""  # once per process

    verdicts.append_label(target, _label("new1"))
    assert legacy.read_bytes() == before  # appends go to the data folder
    assert [json.loads(x)["run_id"] for x in target.read_text(encoding="utf-8").splitlines()] == ["new1"]


def test_no_legacy_file_means_no_output(settings, capsys):
    assert verdicts.legacy_labels(verdicts.labels_path(settings)) == []
    assert capsys.readouterr().err == ""


def test_the_legacy_file_is_not_legacy_when_it_is_the_target(tmp_path, monkeypatch, capsys):
    legacy = _write_legacy([json.dumps(_label("only"), separators=(",", ":"))])
    monkeypatch.setenv("PLATE_LABELS", str(legacy))  # someone pointed the labels at the repo on purpose
    assert verdicts.labels_path() == legacy
    assert verdicts.legacy_labels(legacy) == []
    monkeypatch.delenv("PLATE_LABELS")
    monkeypatch.setenv("PLATE_HOME", str(legacy.parents[1]))  # data home == the old repo folder
    assert verdicts.labels_path() == legacy
    assert capsys.readouterr().err == ""


def test_an_explicit_path_does_not_warn(tmp_path, monkeypatch, capsys):
    _write_legacy([json.dumps(_label("old"), separators=(",", ":"))])
    monkeypatch.setenv("PLATE_LABELS", str(tmp_path / "mine.jsonl"))
    verdicts.labels_path()
    assert capsys.readouterr().err == ""


def test_the_source_has_no_repo_default_for_labels():
    text = Path(verdicts.__file__).read_text(encoding="utf-8")
    assert 'REPO / "calib" / "labels.jsonl"' not in text


# ---------------------------------------------------------------------------- the route lands in the data folder


@pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)
def test_a_canvas_verdict_lands_in_the_data_folder_not_the_repo(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    auth = {"Authorization": f"Bearer {settings.token}"}
    repo_before = (
        (REAL_REPO_LABELS.stat().st_size, REAL_REPO_LABELS.stat().st_mtime_ns)
        if REAL_REPO_LABELS.exists()
        else None
    )
    with TestClient(create_app(settings, FakeEngine(echo=True)), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        made = c.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth
        )
        assert made.status_code == 201, made.text
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = c.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code == 202, r.text
        graph = {
            "nodes": [
                {
                    "id": "p",
                    "kind": "Plate",
                    "packetId": r.json()["packet_id"],
                    "tier": "A",
                    "label": "Plate",
                },
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 200, r.text
        run_id = r.json()["run"]["id"]
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")

        scrub = c.post(
            f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=auth
        )
        assert scrub.status_code == 200, scrub.text
        verdict = {"verdict": "fix", "reasons": ["P1"], "scrub_log_id": scrub.json()["scrub_log_id"]}
        r = c.post(f"/api/v0/runs/{run_id}/verdict", json={**verdict, "rater": "aasish"}, headers=auth)
        assert r.status_code == 200, r.text

    labels = plate_env / "calib" / "labels.jsonl"
    lines = [json.loads(x) for x in labels.read_text(encoding="utf-8").splitlines()]
    assert [(x["run_id"], x["verdict"], x["reasons"], x["rater"]) for x in lines] == [
        (run_id, "fix", ["P1"], "aasish")
    ]
    repo_after = (
        (REAL_REPO_LABELS.stat().st_size, REAL_REPO_LABELS.stat().st_mtime_ns)
        if REAL_REPO_LABELS.exists()
        else None
    )
    assert repo_after == repo_before  # nothing was written under the repo's calib folder
