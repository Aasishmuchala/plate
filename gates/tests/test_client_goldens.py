"""Regression against the client's historical project folders (never copied into the repo).

Every test reaches its files through clientdata.path(), which skips when the file is absent, so a fresh
clone, CI and the render box skip all of these. The numbers are the ones recorded when the original one-off
scripts were run on the delivered work (scout 03).
"""

from __future__ import annotations

import clientdata
import numpy as np
import pytest
from PIL import Image
from PIL.Image import open as pil_open

from plate_gates import align, io, texture, video
from plate_gates.croppair import cut_crops


def _rgb(path) -> Image.Image:
    with pil_open(path) as im:
        return im.convert("RGB")


def test_cut_crops_amphi_n2b_boxes_and_pixels_equal_the_delivered_push_crops():
    src = clientdata.path("amphi-plaza/stills/N2b_relight_locked.png")
    im = _rgb(src)
    assert im.size == (5504, 3072)
    start, end, boxes = cut_crops(im, 3633, 2174, 1.5, 2.3)
    assert boxes == ((1799, 1024, 3668, 2048), (2437, 1507, 2392, 1334))
    for crop, name in ((start, "PUSH_start"), (end, "PUSH_end")):
        delivered = _rgb(clientdata.path(f"amphi-plaza/stills/{name}.png"))
        assert crop.size == delivered.size
        assert np.array_equal(np.asarray(crop), np.asarray(delivered)), name


@pytest.mark.parametrize(
    "take,expected",
    [("S1_NB_a", 0.883), ("S1_NB_b", 0.875), ("S1_GPT_a", 0.762), ("S1_GPT_b", 0.783)],
)
def test_edge_agreement_matches_tools_shot1_qc(take, expected):
    plate = _rgb(clientdata.path("car-entry/refs/vantage_shot1/V1_t493.png"))
    test = _rgb(clientdata.path(f"car-entry/stills/{take}.png"))
    agree, _ = align.edge_agreement(plate, test, width=1920)
    assert agree == pytest.approx(expected, abs=0.01)


def test_v4_take1_lapvar_within_5_percent_of_the_recorded_values():
    clip = clientdata.path("car-entry/clips/v4_take1.mp4")
    info = io.probe(clip)
    got = {t: texture.stats(io.frame_at(clip, t, info=info))["lapvar"] for t in (2.5, 4.8, 7.0)}
    for t in (2.5, 4.8):  # shot 1 recorded 253-261
        assert 253 * 0.95 <= got[t] <= 261 * 1.05, (t, got[t])
    assert got[7.0] == pytest.approx(636, rel=0.05)  # shot 2 recorded 636


def test_s1_take1_legacy_landing_is_10_0_and_9_9():
    clip = clientdata.path("car-entry/clips/S1_take1.mp4")
    start = clientdata.path("car-entry/stills/S1_START_empty_4K.png")
    end = clientdata.path("car-entry/stills/S1_GPT_a.png")
    legacy = video.landing_legacy(clip, start, end)
    assert legacy["first"] == pytest.approx(10.0, abs=0.1)
    assert legacy["last"] == pytest.approx(9.9, abs=0.1)
    assert legacy["last_vs_start"] > legacy["last"]
    # same 16:9 aspect on both sides, so the aspect-correct method records the same numbers on this clip
    fixed = video.landing(clip, start, end)
    assert fixed["first"] == pytest.approx(legacy["first"], abs=0.1)
    assert fixed["last"] == pytest.approx(legacy["last"], abs=0.1)
