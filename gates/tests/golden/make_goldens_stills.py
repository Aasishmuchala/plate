"""Regenerate the still-image goldens and freeze their expected metrics (expected_stills.json).

Sources live in the historical project folders under PLATE_TEST_ROOT (default ~/Desktop/test):
  amphi-plaza/refs/plaza_dusk.png            -> stills/g_plaza_dusk_640.png
  car-entry/refs/vantage_shot1/V1_t493.png   -> stills/g_vantage_v1_t493_640.png
  couple-dining/plates/plate_1.png           -> stills/g_couple_plate1_640.png
Each is downsampled to 640 px wide with cv2.INTER_AREA and saved as PNG (compression 9). The frozen
metrics are, per golden: compare(g, g) (identity), compare(g, roll(g, 3, axis=1)) (3 px shift) and
maskdiff on a centred box (half the width and half the height, i.e. 25% of the area) whose test pixels
are inverted; plus compare(plaza, vantage) as the cross-scene case.

Run from the repo root:  PYTHONPATH=gates .venv/bin/python gates/tests/golden/make_goldens_stills.py
Commit the PNGs and the JSON together; test_golden_stills.py compares live numbers to the frozen ones.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plate_gates.compare import compare, load, maskdiff, write_png  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "stills"
WIDTH = 640

SOURCES = {
    "g_plaza_dusk_640": ("amphi-plaza", "refs", "plaza_dusk.png"),
    "g_vantage_v1_t493_640": ("car-entry", "refs", "vantage_shot1", "V1_t493.png"),
    "g_couple_plate1_640": ("couple-dining", "plates", "plate_1.png"),
}


def test_root() -> Path:
    return Path(os.environ.get("PLATE_TEST_ROOT") or Path.home() / "Desktop" / "test")


def centred_box_case(g: np.ndarray) -> dict:
    """maskdiff of g against itself with a centred half-by-half box inverted."""
    h, w = g.shape[:2]
    y0, y1, x0, x1 = h // 4, h - h // 4, w // 4, w - w // 4
    mask = np.zeros((h, w), np.uint8)
    mask[y0:y1, x0:x1] = 255
    edited = g.copy()
    edited[y0:y1, x0:x1] = 255 - edited[y0:y1, x0:x1]
    return maskdiff(g, edited, mask)


def main() -> None:
    root = test_root()
    OUT.mkdir(parents=True, exist_ok=True)
    goldens: dict[str, np.ndarray] = {}
    for name, parts in SOURCES.items():
        src = root.joinpath(*parts)
        if not src.is_file():
            raise SystemExit(f"missing source {src} (set PLATE_TEST_ROOT)")
        g = load(src, WIDTH)
        write_png(OUT / f"{name}.png", g, compression=9)
        goldens[name] = g
        print(name, g.shape[1], "x", g.shape[0])
    # freeze the metrics from the files as saved, not from the in-memory arrays
    saved = {
        name: cv2.imdecode(np.fromfile(str(OUT / f"{name}.png"), np.uint8), cv2.IMREAD_COLOR)
        for name in goldens
    }
    expected: dict = {"width": WIDTH, "goldens": {}, "cross": {}}
    for name, g in saved.items():
        expected["goldens"][name] = {
            "identity": compare(g, g.copy()),
            "roll3": compare(g, np.roll(g, 3, axis=1)),
            "maskdiff_centre_box": centred_box_case(g),
        }
    expected["cross"]["plaza_vs_vantage"] = compare(saved["g_plaza_dusk_640"], saved["g_vantage_v1_t493_640"])
    with open(HERE / "expected_stills.json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(expected, indent=1) + "\n")
    print("wrote expected_stills.json")


if __name__ == "__main__":
    main()
