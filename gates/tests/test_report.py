"""GateReport: scoring, verdict rule, versioned never-overwriting files, code hash."""

from __future__ import annotations

import json
import re

import numpy as np
import pytest
from blake3 import blake3

from plate_gates import __version__, report
from plate_gates.report import GateReport, metric_code_hash, read, write


def _score(**kw):
    return GateReport.score(
        {"texture.lapvar": 412.0, "registration.residual_px": 0.4, "texture.fine_ratio": 0.5},
        tier="A",
        engine={"id": "nano-banana-2.1", "version": "2026-10"},
        **kw,
    )


def test_fields_and_defaults():
    rep = _score()
    d = rep.model_dump(mode="json")
    assert list(d) == [
        "gate_version",
        "metric_code_hash",
        "threshold_set_id",
        "engine",
        "tier",
        "metrics",
        "checks",
        "verdict",
        "backend_notes",
        "created_utc",
    ]
    assert d["gate_version"] == __version__ == "0.1.0"
    assert d["threshold_set_id"] == "ts-2026.10.17-draft"
    assert d["engine"] == {"id": "nano-banana-2.1", "version": "2026-10"} and d["tier"] == "A"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", d["created_utc"])
    assert d["checks"][0] == {"metric_id": "texture.lapvar", "value": 412.0, "status": "pass"}
    assert GateReport().engine is None


def test_verdict_is_fail_iff_a_hard_check_fails():
    assert _score().verdict == "pass"
    hard = GateReport.score({"texture.lapvar": 3500.0, "texture.fine_ratio": 0.5})
    assert hard.verdict == "fail" and [c.status for c in hard.checks] == ["fail", "pass"]
    soft = GateReport.score({"texture.lapvar": 412.0, "texture.fine_ratio": 0.9})
    assert soft.verdict == "advisory"
    both = GateReport.score({"texture.lapvar": 3500.0, "texture.fine_ratio": 0.9})
    assert both.verdict == "fail"
    assert GateReport().verdict == "advisory"  # nothing scored is not a pass


def test_verdict_cannot_be_forced():
    rep = GateReport(verdict="pass", checks=[{"metric_id": "ral.de00", "value": 4.0, "status": "fail"}])
    assert rep.verdict == "fail"


def test_variants_backend_and_targets_flow_through():
    r = GateReport.score(
        {"gbuffer_edge_recall_r2": 0.87, "video.camera_lock_median_flow_px": 2.0, "video.duration_s": 4.0},
        variants={"gbuffer_edge_recall_r2": "preview"},
        backend="dis",
        targets={"video.duration_s": 5.0},
        backend_notes=["flow backend dis @960"],
    )
    assert [c.status for c in r.checks] == ["pass", "advisory", "fail"]
    assert r.backend_notes == ["flow backend dis @960"] and r.verdict == "fail"
    raft = GateReport.score({"video.camera_lock_median_flow_px": 2.0}, backend="raft")
    assert raft.verdict == "fail"


def test_unknown_metric_ids_raise_instead_of_being_skipped():
    with pytest.raises(KeyError):
        GateReport.score({"made.up": 1.0})


def test_numpy_values_and_nan_become_plain_json():
    rep = GateReport.score(
        {"registration.shift_px": np.float32(1.5)},
        extra_metrics={
            "edge_px": {"n": np.int64(7), "arr": np.arange(3)},
            "nan": float("nan"),
            "ok": np.bool_(True),
        },
    )
    data = json.loads(rep.to_json())  # allow_nan=False would raise on a NaN
    assert data["metrics"]["edge_px"] == {"n": 7, "arr": [0, 1, 2]}
    assert data["metrics"]["nan"] is None and data["metrics"]["ok"] is True
    assert data["metrics"]["registration.shift_px"] == 1.5


def test_metric_code_hash_covers_every_module_once_per_process():
    from pathlib import Path

    here = Path(report.__file__).resolve().parent
    h = blake3()
    for p in sorted(here.glob("*.py"), key=lambda q: q.name):
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    assert metric_code_hash() == h.hexdigest() and re.fullmatch(r"[0-9a-f]{64}", metric_code_hash())
    assert metric_code_hash() is metric_code_hash()  # lru_cache: computed once
    assert GateReport().metric_code_hash == metric_code_hash()


def test_write_numbers_files_and_never_overwrites(tmp_path):
    a = write(_score(), tmp_path, "run_01")
    b = write(_score(), tmp_path, "run_01")
    c = write(_score(), tmp_path, "run_02")
    assert [p.name for p in (a, b, c)] == ["run_01.1.json", "run_01.2.json", "run_02.1.json"]
    assert a.parent == tmp_path / "gates"
    before = a.read_bytes()
    write(_score(), tmp_path, "run_01")
    assert a.read_bytes() == before


def test_write_fills_the_next_free_integer_after_gaps_and_handles_a_lost_race(tmp_path, monkeypatch):
    (tmp_path / "gates").mkdir()
    (tmp_path / "gates" / "r.5.json").write_text("{}", encoding="utf-8", newline="\n")
    assert write(_score(), tmp_path, "r").name == "r.6.json"
    # another writer takes the name between our scan and our create: we move on to the next integer
    real_scan = report.re.compile

    def sneaky(pattern, *a, **k):
        rx = real_scan(pattern, *a, **k)
        if "r" in pattern:
            (tmp_path / "gates" / "r.7.json").write_text("{}", encoding="utf-8", newline="\n")
        return rx

    monkeypatch.setattr(report.re, "compile", sneaky)
    assert write(_score(), tmp_path, "r").name == "r.8.json"


def test_write_is_lf_utf8_and_reads_back(tmp_path):
    p = write(_score(), tmp_path, "run_x", subdir="")
    assert p.parent == tmp_path
    raw = p.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"}\n")
    again = read(p)
    assert again.verdict == "pass" and again.engine.id == "nano-banana-2.1" and again.checks[0].value == 412.0


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "a:b", "", "a b", "../x"])
def test_run_id_is_validated_for_windows_file_names(tmp_path, bad):
    with pytest.raises(ValueError):
        write(_score(), tmp_path, bad)


def test_legacy_gate_report_wraps_free_form_metrics():
    d = report.gate_report({"ssim_gray": 0.9}, engine="e", engine_version="1")
    assert d["engine"] == {"id": "e", "version": "1"} and d["checks"] == [] and d["verdict"] == "advisory"
    assert report.gate_report({"x": 1.0})["engine"] is None
