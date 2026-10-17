"""Committed still goldens (640 px derivatives of real project frames): identity, shift, distinct scenes, frozen metrics.

Regenerate with gates/tests/golden/make_goldens_stills.py (needs PLATE_TEST_ROOT). Floats are frozen at
abs=1e-3 or rel=1e-2 (whichever is looser) so Windows and macOS cv2 builds agree; ints within 0.5%.
The best_shift_long dx/dy of natural images are not frozen: with a +-2 px tolerance the argmax is a
near-tie (recall differs in the 4th decimal between neighbouring shifts), so only recall_r2 is compared.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from plate_gates.compare import compare, load, maskdiff

NAMES = ["g_plaza_dusk_640", "g_vantage_v1_t493_640", "g_couple_plate1_640"]

# These PNGs are derived from client renders, so they are gitignored and never committed.
# They exist only on machines that hold the client projects; elsewhere this module skips.
_STILLS = Path(__file__).resolve().parent / "golden" / "stills"
if not all((_STILLS / f"{n}.png").is_file() for n in NAMES):
    pytest.skip("client-derived still goldens absent (gitignored)", allow_module_level=True)
UNSTABLE = {"dx", "dy"}  # under best_shift_long, see module docstring


@pytest.fixture(scope="module")
def expected(golden_dir: Path) -> dict:
    return json.loads((golden_dir / "expected_stills.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden(golden_dir: Path) -> dict:
    return {n: load(golden_dir / "stills" / f"{n}.png", None) for n in NAMES}


def _close(actual, frozen, where: str, skip_shift_axes: bool) -> None:
    if isinstance(frozen, dict):
        assert isinstance(actual, dict) and set(actual) == set(frozen), where
        for k, v in frozen.items():
            if skip_shift_axes and where.endswith("best_shift_long") and k in UNSTABLE:
                continue
            _close(actual[k], v, f"{where}.{k}", skip_shift_axes)
    elif isinstance(frozen, list):
        assert len(actual) == len(frozen), where
        for i, (a, f) in enumerate(zip(actual, frozen, strict=True)):
            _close(a, f, f"{where}[{i}]", skip_shift_axes)
    elif isinstance(frozen, bool):
        assert actual == frozen, where
    elif isinstance(frozen, int):
        assert abs(int(actual) - frozen) <= 0.005 * max(abs(frozen), 1), f"{where}: {actual} vs {frozen}"
    elif isinstance(frozen, float):
        assert float(actual) == pytest.approx(frozen, abs=1e-3, rel=1e-2), where
    else:
        assert actual == frozen, where


def _box_case(g: np.ndarray) -> dict:
    h, w = g.shape[:2]
    y0, y1, x0, x1 = h // 4, h - h // 4, w // 4, w - w // 4
    mask = np.zeros((h, w), np.uint8)
    mask[y0:y1, x0:x1] = 255
    edited = g.copy()
    edited[y0:y1, x0:x1] = 255 - edited[y0:y1, x0:x1]
    return maskdiff(g, edited, mask)


def test_golden_sizes(golden):
    assert golden["g_plaza_dusk_640"].shape[:2] == (357, 640)
    assert golden["g_vantage_v1_t493_640"].shape[:2] == (360, 640)
    assert golden["g_couple_plate1_640"].shape[:2] == (360, 640)


@pytest.mark.parametrize("name", NAMES)
def test_identity(golden, name):
    g = golden[name]
    res = compare(g, g.copy())
    assert res["ssim_gray"] > 0.999
    assert res["canny_long_r0"]["iou"] > 0.999
    assert res["de00_blur"]["median"] < 1e-6


@pytest.mark.parametrize("name", NAMES)
def test_three_px_shift_stays_within_tolerance(golden, name):
    g = golden[name]
    res = compare(g, np.roll(g, 3, axis=1))
    assert res["canny_long_r4"]["recall"] > 0.95


def test_distinct_scenes_score_as_distinct(golden):
    res = compare(golden["g_plaza_dusk_640"], golden["g_vantage_v1_t493_640"])
    assert res["ssim_gray"] < 0.5
    assert res["canny_long_r2"]["recall"] < 0.6


@pytest.mark.parametrize("name", NAMES)
def test_centre_box_inversion_leaves_outside_untouched(golden, name):
    md = _box_case(golden[name])
    assert md["outside"]["frac_identical"] == 1.0
    assert md["inside"]["frac_identical"] < 0.05
    assert md["mask_frac"] == pytest.approx(0.25, abs=0.005)


@pytest.mark.parametrize("name", NAMES)
def test_frozen_metrics(golden, expected, name):
    g = golden[name]
    frozen = expected["goldens"][name]
    _close(compare(g, g.copy()), frozen["identity"], f"{name}.identity", skip_shift_axes=False)
    _close(compare(g, np.roll(g, 3, axis=1)), frozen["roll3"], f"{name}.roll3", skip_shift_axes=True)
    _close(_box_case(g), frozen["maskdiff_centre_box"], f"{name}.maskdiff", skip_shift_axes=False)


def test_frozen_cross_metrics(golden, expected):
    res = compare(golden["g_plaza_dusk_640"], golden["g_vantage_v1_t493_640"])
    _close(res, expected["cross"]["plaza_vs_vantage"], "cross.plaza_vs_vantage", skip_shift_axes=True)
