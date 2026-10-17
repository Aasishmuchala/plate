"""The built-in selftest, the compatibility shim, and the versioned gate report."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from plate_gates import __version__, thresholds
from plate_gates.report import gate_report, metric_code_hash
from plate_gates.selftest import run_selftest
from plate_gates.thresholds import DEFAULT_THRESHOLDS_ID

GATES_DIR = Path(__file__).resolve().parent.parent


def test_selftest_passes():
    assert run_selftest(verbose=False) is True


def test_selftest_prints_verdict(capsys):
    assert run_selftest(verbose=True) is True
    assert capsys.readouterr().out.strip().splitlines()[-1] == "SELFTEST PASS"


@pytest.mark.parametrize("module", ["plate_gates", "plate_gates.fidelity_bench"])
def test_cli_selftest_exit_code_and_last_line(module, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(GATES_DIR) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", module, "selftest"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "SELFTEST PASS"


def test_metric_code_hash_is_stable_blake3_hex():
    h = metric_code_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h == metric_code_hash()


def test_gate_report_envelope():
    rep = gate_report({"ssim_gray": 0.99}, engine_version="fake-1", engine="fake")
    assert set(rep) == {
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
    }
    assert rep["gate_version"] == __version__
    assert rep["metric_code_hash"] == metric_code_hash()
    assert rep["threshold_set_id"] == DEFAULT_THRESHOLDS_ID
    assert rep["engine"] == {"id": "fake", "version": "fake-1"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rep["created_utc"])
    assert rep["metrics"] == {"ssim_gray": 0.99}
    assert rep["checks"] == [] and rep["verdict"] == "advisory"  # nothing was scored
    assert gate_report({})["engine"] is None


def test_thresholds_decision_14_values_with_scout_corrections():
    ts = thresholds.load()
    assert ts.id == DEFAULT_THRESHOLDS_ID == "ts-2026.10.17-draft"
    assert ts.get("registration.residual_px").value == 1.0
    assert ts.get("gbuffer_edge_recall_r2").variants == {"delivery": 0.90, "preview": 0.85}
    assert ts.get("texture.lapvar").range == (300, 600) and ts.get("texture.lapvar").status == "hard"
    fine = ts.get("texture.fine_ratio")
    assert fine.range == (0.40, 0.60) and fine.status == "calibration_needed"
    assert ts.get("video.first_frame").value == 12 and ts.get("video.last_frame").value == 16
    assert ts.get("invention_flags").value == 0 and ts.get("invention_flags").status == "hard"
