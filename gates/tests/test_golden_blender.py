"""Blender-rendered gate goldens: committed, derived from the fixture packets, always run.

The client-derived goldens (golden/stills, golden/texture) are gitignored and skip on a fresh clone; these do
not. Pairs, masks, the 1 s dolly clip and golden/blender/expected.json are committed, so the compare, maskdiff,
composite_masked, texture, landing, camera_lock, flicker and duration gates are exercised on every checkout with
no client data. Regenerate with golden/make_goldens_blender.py or `pytest gates/tests/test_golden_blender.py
--update-goldens` (CI never passes the flag).

Each frozen section is paired with semantic assertions that hold whatever the frozen numbers are, so a
regenerated expected.json cannot bless a broken metric. The only skips are the three sections that need the
ffmpeg binary (landing, flicker, duration), as in every other video test; nothing here reads client data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import make_goldens_blender as G
import numpy as np
import procedural as P
import pytest

from plate_gates import io as pio
from plate_gates import thresholds
from plate_gates.io import ffmpeg
from plate_gates.video import camera_lock_phasecorr, probe, write_test_clip

REPO = Path(__file__).resolve().parents[2]
BLENDER_REL = "gates/tests/golden/blender"

HAVE_FFMPEG = ffmpeg() is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not found (set PLATE_FFMPEG)")

COMPARE_CASES = ["identity", "shift3", "blur2", "window_added", "material_b6", "pad_2391", "cross_view"]
MASKDIFF_CASES = ["window_added_in_wall", "material_b6_in_panel"]
FROZEN_CASES = {
    "compare": COMPARE_CASES,
    "maskdiff": MASKDIFF_CASES,
    "material": ["b6_in_panel_7016"],
    "composite_masked": ["material_b6_brighter_ai"],
    "texture": ["soft", "crunch"],
    "pad": ["pad_2391"],
    "camera_lock": ["dolly_moves"],
    "landing": ["dolly"],
    "flicker": ["dolly"],
    "duration": ["dolly_1s"],
}
FFMPEG_SECTIONS = {"landing", "flicker", "duration"}


# --- the files are in git -----------------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError:
        return None


def test_every_golden_file_exists():
    missing = [n for n in G.COMMITTED_FILES if not (G.OUT / n).is_file()]
    assert not missing, f"missing from {BLENDER_REL}: {missing} (run make_goldens_blender.py)"


def test_goldens_are_not_gitignored_and_are_listed_by_git():
    """Nobody can gitignore the goldens silently: git must list every file (tracked, or untracked but not
    ignored) and no ignore pattern may match it. Without git (a source tarball) only existence is checked."""
    probe = _git("rev-parse", "--is-inside-work-tree")
    if probe is None or probe.returncode != 0:
        assert all((G.OUT / n).is_file() for n in G.COMMITTED_FILES)
        return
    listed = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", BLENDER_REL)
    assert listed is not None and listed.returncode == 0, listed.stderr if listed else "git vanished"
    names = {Path(line).name for line in listed.stdout.splitlines()}
    missing = [n for n in G.COMMITTED_FILES if n not in names]
    assert not missing, (
        f"git does not list {missing}: gitignored? (git ls-files --cached --others --exclude-standard)"
    )
    for name in G.COMMITTED_FILES:
        res = _git("check-ignore", "--no-index", "-q", "--", f"{BLENDER_REL}/{name}")
        assert res is not None and res.returncode == 1, f"{BLENDER_REL}/{name} matches an ignore pattern"


def test_size_caps():
    sizes = G.sizes_kb()
    too_big = {n: round(kb) for n, kb in sizes.items() if n.endswith(".png") and kb > G.PNG_CAP_KB}
    assert not too_big, f"PNGs over {G.PNG_CAP_KB} KB: {too_big}"
    assert sum(sizes.values()) <= G.TOTAL_CAP_KB, f"{sum(sizes.values()):.0f} KB over {G.TOTAL_CAP_KB}"


def test_pngs_are_8bit_three_channel_at_640_wide():
    for name in G.PNG_FILES:
        if name.endswith("_mask.png"):
            im = cv2.imdecode(np.fromfile(str(G.OUT / name), np.uint8), cv2.IMREAD_UNCHANGED)
            assert im.dtype == np.uint8 and im.shape == (360, 640), name
            assert set(np.unique(im)) <= {0, 255}, name
        elif name.startswith("dolly_"):
            im = G.read_png(name)
            assert im.dtype == np.uint8 and im.shape == (180, 320, 3), name
        else:
            im = G.read_png(name)
            assert im.dtype == np.uint8 and im.shape == (360, 640, 3), name


def test_goldens_still_derive_from_the_fixture_packets():
    """Provenance, not bit-equality (the denoise is not reproducible across OpenCV builds): the stored bases are
    the packets' display masters up to the NLM denoise, so a re-rendered fixture makes this fail loudly."""
    for case, room in (("identity", "room_a"), ("pad_2391", "room_b")):
        src = pio.to_uint8(pio.load_rgb(G.PACKETS / room / "display.tif"))
        stored = cv2.cvtColor(G.read_png(f"{case}_a.png"), cv2.COLOR_BGR2RGB)
        assert stored.shape == src.shape
        assert float(np.abs(stored.astype(np.int16) - src).mean()) < 8.0, f"{case} no longer matches {room}"


# --- frozen metrics -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def computed(update_goldens):
    if update_goldens:
        if not HAVE_FFMPEG:
            pytest.fail("--update-goldens needs ffmpeg for the dolly clip and the video sections")
        G.build()
        data = G.sections(G.OUT)
        G.write_expected(data)
        return data
    return G.sections(G.OUT, with_video=HAVE_FFMPEG)


@pytest.fixture(scope="module")
def expected(computed):
    if not G.EXPECTED.is_file():
        pytest.fail("blender/expected.json missing: run make_goldens_blender.py")
    return G.read_expected()


FROZEN_IDS = [(s, c) for s, cases in FROZEN_CASES.items() for c in cases]


@pytest.mark.parametrize(
    ("section", "case"),
    [pytest.param(s, c, marks=needs_ffmpeg if s in FFMPEG_SECTIONS else ()) for s, c in FROZEN_IDS],
    ids=[f"{s}.{c}" for s, c in FROZEN_IDS],
)
def test_frozen(computed, expected, section, case):
    assert case in expected[section], f"{section}.{case} not frozen"
    bad = P.compare_to_expected(computed[section][case], expected[section][case], f"{section}.{case}")
    assert not bad, "\n".join(bad)


def test_frozen_file_lists_exactly_the_cases_the_suite_checks(computed, expected):
    assert set(expected) - {"_tolerances"} == set(FROZEN_CASES)
    for section in computed:
        assert set(expected[section]) == set(FROZEN_CASES[section]), section
        assert set(computed[section]) == set(FROZEN_CASES[section]), section


def test_frozen_file_documents_the_tolerances_the_comparison_uses(expected):
    assert expected["_tolerances"] == P.TOLERANCE_DOC


# --- semantic assertions that do not depend on the frozen numbers ---------------------------------------------


def test_identity_is_perfect(computed):
    c = computed["compare"]["identity"]
    assert c["ssim_gray"] > 0.999 and c["long_recall_r0"] > 0.999 and c["long_precision_r0"] > 0.999
    assert c["de00_blur_median"] < 1e-6 and c["de00_blur_p90"] < 1e-6
    # r=2 dilation makes every shift in -2..2 a tie for the argmax, so only its recall is meaningful
    assert c["best_shift_recall_r2"] == 1.0 and abs(c["best_shift_dx"]) <= 2 and abs(c["best_shift_dy"]) <= 2


def test_three_px_shift_is_found_exactly_and_forgiven_only_at_r4(computed):
    c = computed["compare"]["shift3"]
    assert (c["best_shift_dx"], c["best_shift_dy"]) == (-3, 0)
    assert c["long_recall_r4"] > 0.95 and c["long_recall_r0"] < 0.5
    assert c["long_precision_r4"] > 0.8 and c["long_precision_r2"] < 0.8


def test_blur_loses_long_edge_recall_but_not_precision(computed):
    c = computed["compare"]["blur2"]
    assert c["long_recall_r0"] < 0.5 and c["long_precision_r2"] > 0.95
    assert c["ssim_gray"] < 0.98 and c["ssim_gray_blur8"] > 0.99  # texture lost, layout kept


def test_added_window_costs_precision_not_recall(computed):
    c = computed["compare"]["window_added"]
    assert c["long_precision_r2"] < 0.9 and c["long_recall_r2"] >= 0.95


def test_window_changes_only_pixels_inside_the_wall_mask(computed):
    a, b = G.read_png("window_added_a.png"), G.read_png("window_added_b.png")
    wall = G.read_mask("window_added_mask.png")
    changed = np.any(a != b, axis=2)
    assert changed.any() and not changed[~wall].any()
    md = computed["maskdiff"]["window_added_in_wall"]
    assert md["outside_frac_identical"] == 1.0 and md["outside_de00_p99"] == 0.0
    assert 0.5 < md["inside_frac_identical"] < 1.0  # a window is part of the wall, not all of it


def test_material_b6_median_de00_inside_the_mask_is_six_within_half_a_unit(computed):
    m = computed["material"]["b6_in_panel_7016"]
    assert m["de00_inside_median"] == pytest.approx(6.0, abs=0.5)
    assert m["de76_inside_median"] == pytest.approx(6.0, abs=0.5)  # the Lab distance of a +6 b* shift
    assert m["db_star_median"] == pytest.approx(6.0, abs=0.5)


def test_material_b6_leaves_everything_outside_the_mask_untouched(computed):
    md = computed["maskdiff"]["material_b6_in_panel"]
    assert md["outside_frac_identical"] == 1.0 and md["outside_de00_p99"] == 0.0
    assert md["inside_frac_identical"] < 0.05
    c = computed["compare"]["material_b6"]
    assert c["long_recall_r2"] > 0.99 and c["ssim_gray"] > 0.99  # colour moved, structure did not


def test_composite_outside_is_the_plate_and_tone_match_helps_the_ring(computed):
    c = computed["composite_masked"]["material_b6_brighter_ai"]
    assert c["outside_identical"] is True and c["outside_max_abs_diff"] == 0.0
    assert c["de00_ring_matched_mean"] < 0.75 * c["de00_ring_raw_mean"]


def test_crunch_reads_far_sharper_than_the_render_at_1024(computed):
    soft, crunch = computed["texture"]["soft"], computed["texture"]["crunch"]
    assert soft["eval_width"] == crunch["eval_width"] == 1024
    assert crunch["lapvar"] > 3 * soft["lapvar"] and crunch["fine_ratio"] > soft["fine_ratio"]


def test_crunch_is_deterministic_and_changes_the_pixels():
    a = G.read_png("texture_crunch_a.png")
    c1, c2 = G.crunch(a), G.crunch(a)
    assert np.array_equal(c1, c2) and not np.array_equal(c1, a)


def test_pad_to_2391_is_edge_replication_and_unpad_is_lossless(computed):
    p = computed["pad"]["pad_2391"]
    assert p["pad_aspect"] == pytest.approx(2.39, abs=0.01) and p["pad_each_side"] == 110
    assert p["edge_replicated"] is True and p["unpad_identical"] is True
    assert p["stored_roundtrip_identical"] is True
    c = computed["compare"]["pad_2391"]
    assert c["ssim_gray"] > 0.999 and c["de00_blur_p90"] < 1e-6


def test_two_cameras_of_one_scene_score_as_distinct_views(computed):
    c = computed["compare"]["cross_view"]
    assert c["long_recall_r2"] < 0.2 and c["de00_blur_median"] > 10


def test_dolly_clip_is_one_second_at_24_fps_with_24_frames():
    info = probe(G.OUT / "dolly.mp4")
    assert (info["width"], info["height"]) == (320, 180)
    assert info["nb_frames"] == 24 and info["fps"] == pytest.approx(24.0)


def test_camera_lock_sees_the_dolly_in_every_textured_region(computed):
    c = computed["camera_lock"]["dolly_moves"]
    assert c["verdict"] == "moved" and c["n_moved"] == len(G.DOLLY_REGIONS)


def test_camera_lock_calls_a_static_clip_locked(tmp_path):
    still = cv2.cvtColor(G.read_png("dolly_first.png"), cv2.COLOR_BGR2RGB)
    clip = write_test_clip(tmp_path / "static.mp4", [still] * 24, fps=24)
    res = camera_lock_phasecorr(clip, G.DOLLY_REGIONS, samples=4)
    assert res["verdict"] == "locked" and not any(r["moved"] for r in res["regions"])


@needs_ffmpeg
def test_landing_meets_the_thresholds_and_last_is_closer_to_end_than_start(computed):
    c = computed["landing"]["dolly"]
    assert thresholds.check("video.first_frame", c["first"])[0] == "pass"
    assert thresholds.check("video.last_frame", c["last"])[0] == "pass"
    assert c["last_vs_start"] > 5 * c["last"]  # a real dolly: the last frame is nowhere near the start still


@needs_ffmpeg
def test_flicker_is_low_and_duration_check_gates(computed):
    f = computed["flicker"]["dolly"]
    assert f["frames"] == 24 and f["mean_abs_luma_delta"] < 1.0 and f["p95"] < 2.0
    d = computed["duration"]["dolly_1s"]
    assert d["duration_s"] == pytest.approx(1.0, abs=0.01) and d["nb_frames"] == 24
    assert d["ok_for_1s"] is True and d["ok_for_2s"] is False
