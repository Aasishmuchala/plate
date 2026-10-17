"""The built-in selftest, the compatibility shim, and the versioned gate report."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from plate_gates import __version__
from plate_gates.report import gate_report, metric_code_hash
from plate_gates.selftest import run_selftest
from plate_gates.thresholds import DEFAULT_THRESHOLDS_ID, THRESHOLDS

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
    rep = gate_report({"ssim_gray": 0.99}, engine_version="fake-1")
    assert set(rep) == {
        "gate_version",
        "metric_code_hash",
        "thresholds_id",
        "engine_version",
        "created_utc",
        "metrics",
    }
    assert rep["gate_version"] == __version__
    assert rep["metric_code_hash"] == metric_code_hash()
    assert rep["thresholds_id"] == DEFAULT_THRESHOLDS_ID
    assert rep["engine_version"] == "fake-1"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rep["created_utc"])
    assert rep["metrics"] == {"ssim_gray": 0.99}
    assert gate_report({})["engine_version"] is None


def test_thresholds_decision_14_values():
    t = THRESHOLDS[DEFAULT_THRESHOLDS_ID]
    assert t["registration_residual_px_max"] == 1.0
    assert t["gbuffer_edge_recall_r2_delivery_min"] == 0.90
    assert t["gbuffer_edge_recall_r2_preview_min"] == 0.85
    assert t["texture_lapvar_range"] == [300, 600]
    assert t["texture_fine_ratio_range"] == [0.35, 0.45]
    assert t["video_first_frame_max_255"] == 8 and t["video_last_frame_max_255"] == 14
    assert t["invention_flags_architecture_max"] == 0
