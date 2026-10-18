"""The daemon's FakeEngine in echo mode (router plan T61): `PLATE_FAKE_ENGINE=1`, `echo`, `shift_px`.

Unit tests drive the engine through the adapter protocol; the pipeline tests run it on the committed Blender
packet `fixtures/packets/room_a`, where an echoed result registers to the plate (DONE) and a result shifted 6 px
is camera drift (GATE_FAIL). The last test is the other half of the FLUX 3 sizes change: a quote for
`flux-3-image` no longer answers `no_sizes`.

T130: with a journal the echo sources persist beside it (`<journal>.echo/<job_id>.png`) and are reloaded on
construction, so a restarted fake echoes the same bytes; the `ResultExpired` path stays for a job whose file is
gone. The live-daemon kill in FETCHING is in `test_stress_wave12_worker.py`.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.adapter import ResultExpired
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import ECHO_KEEP, FLAG_NAMES, FakeEngine, render, shift_right

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
DRIFT_PX = 12  # past the +-8 px registration window (registration.shift_px <= 8)

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


def png_of(img: np.ndarray) -> bytes:
    ok, enc = cv2.imencode(".png", img)
    assert ok
    return bytes(enc)


def decode(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img is not None
    return img


def ramp(w: int = 24, h: int = 6, dtype=np.uint8) -> np.ndarray:
    """Every column different, so a shift is visible: column x holds the value 10 * x in all channels."""
    top = 255 if dtype == np.uint8 else 65535
    col = (np.arange(w)[None, :, None] * (top // w)).astype(dtype)
    return np.broadcast_to(col, (h, w, 3)).copy()


def payload_of(png: bytes | None, **extra) -> dict:
    inputs = {"prompt": "dusk"}
    if png is not None:
        inputs["image"] = {"b64": base64.b64encode(png).decode("ascii"), "mime": "image/png"}
    return {"inputs": inputs, "params": {}, **extra}


def run_job(engine: FakeEngine, payload: dict, key: str = "k1") -> bytes:
    job = engine.submit(payload, key)
    for _ in range(10):
        got = engine.poll(job)
        if got.status == "succeeded":
            return engine.fetch(got.result_url)
    raise AssertionError("the fake never finished")


# ---------------------------------------------------------------------------- PLATE_FAKE_ENGINE


@pytest.mark.parametrize("word", ["1", "true", "TRUE", "yes", "On", " 1 "])
def test_truthy_words_mean_the_echoing_fake(monkeypatch, word):
    monkeypatch.setenv("PLATE_FAKE_ENGINE", word)
    e = FakeEngine.from_env(journal=None)
    assert e.echo is True and e.shift_px == 0 and e.behaviour == "ok"


@pytest.mark.parametrize("word", [None, "", "  ", "0", "false", "False"])
def test_falsy_words_and_unset_mean_the_defaults(monkeypatch, word):
    if word is None:
        monkeypatch.delenv("PLATE_FAKE_ENGINE", raising=False)
    else:
        monkeypatch.setenv("PLATE_FAKE_ENGINE", word)
    e = FakeEngine.from_env(journal=None)
    assert e.echo is False and e.shift_px == 0 and e.behaviour == "ok" and e.size == (64, 64)


def test_a_json_object_sets_the_flags_as_before(monkeypatch):
    monkeypatch.setenv("PLATE_FAKE_ENGINE", json.dumps({"echo": True, "shift_px": 3, "behaviour": "slow"}))
    e = FakeEngine.from_env(journal=None)
    assert (e.echo, e.shift_px, e.behaviour) == (True, 3, "slow")
    assert {"echo", "shift_px"} <= set(FLAG_NAMES)


@pytest.mark.parametrize("bad", ['{"nope": 1}', "[1]", "{not json", "2"])
def test_a_bad_value_is_a_value_error(monkeypatch, bad):
    monkeypatch.setenv("PLATE_FAKE_ENGINE", bad)
    with pytest.raises(ValueError, match="PLATE_FAKE_ENGINE"):
        FakeEngine.from_env(journal=None)


# ---------------------------------------------------------------------------- echo and shift


def test_echo_returns_the_submitted_png_byte_for_byte():
    png = png_of(ramp())
    e = FakeEngine(echo=True)
    assert run_job(e, payload_of(png)) == png


def test_without_echo_the_result_is_the_deterministic_noise():
    png = png_of(ramp())
    e = FakeEngine()
    payload = payload_of(png)
    out = run_job(e, payload)
    assert out != png and decode(out).shape == (64, 64, 3)
    assert out == e.render(payload)  # the seed's behaviour, untouched


def test_a_payload_with_no_image_gets_the_noise_even_when_echoing():
    e = FakeEngine(echo=True)
    payload = payload_of(None)
    assert run_job(e, payload) == render(0, *_digest(payload))


def _digest(payload):
    from plate_daemon.engine_fake import payload_digest

    return payload_digest(payload)


def test_shift_px_moves_the_image_right_and_replicates_the_left_edge():
    img = ramp()
    e = FakeEngine(echo=True, shift_px=6)
    out = decode(run_job(e, payload_of(png_of(img))))
    assert out.shape == img.shape and out.dtype == img.dtype
    assert np.array_equal(out[:, 6:], img[:, :-6])
    assert all(np.array_equal(out[:, i], img[:, 0]) for i in range(6))  # edge replication, not a wrap-around
    assert not np.array_equal(out, img)


def test_a_negative_shift_moves_left_and_replicates_the_right_edge():
    img = ramp()
    out = decode(shift_right(png_of(img), -5))
    assert np.array_equal(out[:, :-5], img[:, 5:])
    assert all(np.array_equal(out[:, -1 - i], img[:, -1]) for i in range(5))


def test_a_shift_wider_than_the_image_is_a_flat_edge_column():
    img = ramp(w=8)
    for px in (8, 50, -50):
        out = decode(shift_right(png_of(img), px))
        edge = img[:, 0] if px > 0 else img[:, -1]
        assert out.shape == img.shape and all(np.array_equal(out[:, i], edge) for i in range(8))


def test_zero_shift_does_not_re_encode():
    png = png_of(ramp())
    assert shift_right(png, 0) is png


def test_sixteen_bit_pngs_keep_their_depth():
    img = ramp(dtype=np.uint16)
    out = decode(run_job(FakeEngine(echo=True, shift_px=2), payload_of(png_of(img))))
    assert out.dtype == np.uint16 and np.array_equal(out[:, 2:], img[:, :-2])


def test_a_corrupt_png_cannot_be_shifted():
    with pytest.raises(ValueError, match="decodable"):
        shift_right(b"not a png", 3)


# ---------------------------------------------------------------------------- journal, restart, memory


def test_the_journal_keeps_the_hashes_not_the_image(tmp_path):
    journal = tmp_path / "fake.json"
    png = png_of(ramp(w=64, h=64))
    e = FakeEngine(echo=True, journal=journal)
    run_job(e, payload_of(png))
    text = journal.read_text(encoding="utf-8")
    assert base64.b64encode(png).decode("ascii")[:40] not in text
    job = next(iter(json.loads(text)["jobs"].values()))
    assert len(job["inputs_hash"]) == 64 and len(job["params_hash"]) == 64 and job["has_image"] is True


def test_a_restarted_engine_echoes_the_same_bytes_from_beside_the_journal(tmp_path):
    """T130 (was: the restart lost the image and needed a resubmit): the source is persisted and reloaded."""
    png = png_of(ramp())
    journal = tmp_path / "fake.json"
    e = FakeEngine(echo=True, shift_px=4, journal=journal)
    job = e.submit(payload_of(png), "k1")
    assert (tmp_path / "fake.json.echo" / f"{job}.png").read_bytes() == png
    first = e.fetch(f"fake://results/{job}")
    again = e.restarted()
    assert again.echo is True and again.shift_px == 4 and job in again._echo_src  # reloaded on construction
    assert again.fetch(f"fake://results/{job}") == first
    out = decode(first)
    assert np.array_equal(out[:, 4:], decode(png)[:, :-4])
    # A brand-new instance over the same journal (a restarted daemon) echoes the same bytes too.
    assert FakeEngine(echo=True, shift_px=4, journal=journal).fetch(f"fake://results/{job}") == first
    # The re-submit of the same key still finds the same job and does not rewrite the file.
    assert again.submit(payload_of(png), "k1") == job and again.job_count == 1


def test_a_job_whose_echo_file_is_gone_still_raises_result_expired(tmp_path):
    png = png_of(ramp())
    e = FakeEngine(echo=True, journal=tmp_path / "fake.json")
    job = e.submit(payload_of(png), "k1")
    (tmp_path / "fake.json.echo" / f"{job}.png").unlink()
    with pytest.raises(ResultExpired, match="resubmit"):
        FakeEngine(echo=True, journal=tmp_path / "fake.json").fetch(f"fake://results/{job}")


def test_the_persisted_echo_sources_are_bounded_like_the_memory_ones(tmp_path):
    png = png_of(ramp())
    e = FakeEngine(echo=True, journal=tmp_path / "fake.json")
    jobs = [e.submit(payload_of(png), f"k{i}") for i in range(ECHO_KEEP + 3)]
    folder = tmp_path / "fake.json.echo"
    assert sorted(p.stem for p in folder.glob("*.png")) == sorted(jobs[-ECHO_KEEP:])
    assert not list(folder.glob("*.tmp")) and not list(folder.glob(".*"))  # atomic writes leave no temp files
    with pytest.raises(ResultExpired):
        FakeEngine(echo=True, journal=tmp_path / "fake.json").fetch(f"fake://results/{jobs[0]}")


def test_without_a_journal_nothing_is_written_and_a_restart_keeps_the_provider_copy(tmp_path):
    png = png_of(ramp())
    e = FakeEngine(echo=True)
    job = e.submit(payload_of(png), "k1")
    assert e.echo_dir is None and list(tmp_path.iterdir()) == []
    assert e.restarted().fetch(f"fake://results/{job}") == png


def test_only_the_newest_jobs_are_held_in_memory():
    png = png_of(ramp())
    e = FakeEngine(echo=True)
    first = e.submit(payload_of(png), "first")
    for i in range(ECHO_KEEP):
        e.submit(payload_of(png), f"k{i}")
    with pytest.raises(ResultExpired):
        e.fetch(f"fake://results/{first}")


def test_text_output_still_wins_over_echo():
    e = FakeEngine(echo=True, output="text")
    assert run_job(e, payload_of(png_of(ramp()))).startswith(b"PLATE-FAKE-RESULT")


# ---------------------------------------------------------------------------- through the pipeline on room_a


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _make(c: TestClient, auth: dict, **body) -> tuple[dict, dict]:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth).status_code == 200
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    return run, c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()


@needs_room_a
def test_an_echo_run_ends_done_and_registers_to_the_plate(sync_settings, auth):
    engine = FakeEngine(echo=True)
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        run, rep = _make(c, auth, engine_id="fake")
        assert run["state"] == "DONE", run.get("error")
        assert rep["metrics"]["registration"]["shift_px"] <= 0.5
        assert rep["metrics"]["registration"]["camera_drift"] is False
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert engine.submit_count == 1 and engine.call_count("fetch") == 1


@needs_room_a
def test_a_shift_past_the_search_window_is_camera_drift_and_a_gate_fail(sync_settings, auth):
    with _client(sync_settings, FakeEngine(echo=True, shift_px=DRIFT_PX)) as c:
        _setup(c, auth)
        run, rep = _make(c, auth, engine_id="fake")
        assert run["state"] == "GATE_FAIL" and run["exportable"] is False
        assert rep["verdict"] == "fail"
        assert rep["metrics"]["registration"]["camera_drift"] is True
        failed = {ch["metric_id"] for ch in rep["checks"] if ch["status"] == "fail"}
        assert "registration.shift_px" in failed
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True  # the plate is safe even so


@needs_room_a
def test_a_small_shift_is_registered_away_and_still_measured(sync_settings, auth):
    """6 px is inside the +-8 px search window: ingress finds it (shift_px ~ 6), warps it back and the take
    passes, so it is not drift. The plan's 'shift_px=6 -> camera_drift' does not hold against the shipped
    threshold (registration.shift_px <= 8); DRIFT_PX is what fails."""
    with _client(sync_settings, FakeEngine(echo=True, shift_px=6)) as c:
        _setup(c, auth)
        run, rep = _make(c, auth, engine_id="fake")
        reg = rep["metrics"]["registration"]
        assert run["state"] == "DONE", run.get("error")
        assert abs(reg["shift_px"] - 6.0) < 0.5 and reg["warp_applied"] is True
        assert reg["camera_drift"] is False and reg["residual_px"] <= 0.5


@needs_room_a
def test_the_daemon_default_engine_follows_plate_fake_engine(sync_settings, auth, monkeypatch):
    """No engine passed to create_app: the app builds the in-process fake from the environment, as
    `python -m plate_daemon` does, and `PLATE_FAKE_ENGINE=1` means the echoing one."""
    monkeypatch.setenv("PLATE_FAKE_ENGINE", "1")
    with _client(sync_settings) as c:
        assert c.app.state.engine.echo is True
        _setup(c, auth)
        run, rep = _make(c, auth, engine_id="fake")
        assert run["state"] == "DONE", run.get("error")
        assert rep["metrics"]["registration"]["shift_px"] <= 0.5


@needs_room_a
def test_a_flux_quote_no_longer_answers_no_sizes(sync_settings, auth):
    with _client(sync_settings, FakeEngine(echo=True)) as c:
        _setup(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote",
            json={"engine_id": "flux-3-image", "prompt": "late blue hour"},
            headers=auth,
        )
        assert r.json().get("error") != "no_sizes", r.text
        assert r.status_code == 200, r.text
        assert r.json()["quote"]["custody_grade"] == "D" and r.json()["run"]["state"] == "QUOTED"
