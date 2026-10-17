"""Wave 8 gate wiring (router plan T60): the feather band is advisory, every check says what it was held to,
and `flagged_points` names the boxes to look at first.

Real pixels: the committed Blender packet `fixtures/packets/room_a` (skipped until it is built) and the
`window_added` synthetic of `fixtures/synthetic`. The Tier A inputs of room_a (edge map, material
Cryptomatte) are read once and handed to `run_gates`, as the pipeline does.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from fixtures.synthetic import make_pair

from plate_daemon import gating
from plate_gates import gbuffer, invention, thresholds
from plate_gates.gbuffer import EdgeParams

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
REG_OK = {"shift_px": 0.0, "residual_px": 0.1, "registered": True, "camera_drift": False}
DE_METRICS = ("outside_mask.de00", "feather_band.de00_p99", "material.de00_p90")

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


def _inside(p, w, h):
    return p.x >= 0 and p.y >= 0 and p.w > 0 and p.h > 0 and p.x + p.w <= w and p.y + p.h <= h


# ---------------------------------------------------------------------------- room_a


@pytest.fixture(scope="module")
def room_a():
    if not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    from plate_pixels import imageio
    from plate_pixels.exr import read_cryptomatte

    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    plate = np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    edges = gbuffer.from_packet(ROOM_A, manifest=manifest)
    material = read_cryptomatte(ROOM_A / manifest["files"]["beauty_exr"]["path"], manifest, "material")
    return {"plate": plate, "legend": legend, "edges": edges, "material": material, "manifest": manifest}


def _run(room, out, mask=None, registration=REG_OK):
    return gating.run_gates(
        room["plate"],
        out,
        {"path": ROOM_A, "manifest": room["manifest"]},
        room["legend"],
        "A",
        {"id": "echo", "version": "t60"},
        mask=mask,
        registration=registration,
        edges=room["edges"],
        crypto_material=room["material"],
    )


def _box_mask(room):
    mask = np.zeros(room["plate"].shape[:2], dtype=bool)
    mask[40:200, 300:600] = True
    return mask


def _brighten(img, mask, amount):
    out = img.copy()
    out[mask] = np.clip(out[mask].astype(np.int64) + amount, 0, 65535).astype(np.uint16)
    return out


@needs_room_a
def test_an_echo_output_keeps_the_feather_band_advisory_and_does_not_fail(room_a):
    rep = _run(room_a, room_a["plate"].copy(), mask=_box_mask(room_a))
    by = {c.metric_id: c for c in rep.checks}
    assert by["feather_band.de00_p99"].status == "advisory"
    assert "feather band: advisory until calibration (T23) sets a bound" in rep.backend_notes
    assert rep.verdict != "fail" and rep.flagged_points == []
    # the registered status is whatever the active set says; the check is forced either way
    assert (
        rep.thresholds["feather_band.de00_p99"].status
        == thresholds.load().get("feather_band.de00_p99").status
    )


@needs_room_a
def test_every_check_on_a_registered_id_says_what_it_was_held_to(room_a):
    rep = _run(room_a, room_a["plate"].copy(), mask=_box_mask(room_a))
    ts = thresholds.load()
    by = {c.metric_id: c for c in rep.checks}
    assert (by["registration.residual_px"].threshold, by["registration.residual_px"].unit) == (1.0, "px")
    assert by["registration.residual_px"].op == "le"
    assert by["gbuffer_edge_recall_r2"].threshold == 0.90  # the delivery bound the gate used
    assert by["texture.lapvar"].threshold == [300.0, 600.0] and by["texture.lapvar"].op == "range"
    registered = [c for c in rep.checks if c.metric_id in ts.metrics]
    assert len(registered) == len(rep.checks) > 10
    for c in registered:
        assert c.threshold is not None and c.unit and c.op, c.metric_id
        assert c.metric_id in rep.thresholds
    # a round trip keeps them
    back = rep.model_validate(rep.model_dump(mode="json"))
    assert [c.threshold for c in back.checks] == [c.threshold for c in rep.checks]


@needs_room_a
def test_a_feather_miss_stays_advisory_and_names_its_worst_tiles(room_a):
    mask = _box_mask(room_a)
    rep = _run(room_a, _brighten(room_a["plate"], mask, 3000), mask=mask)
    by = {c.metric_id: c for c in rep.checks}
    feather = by["feather_band.de00_p99"]
    assert feather.value > 1.0 and feather.status == "advisory"  # past the 1.0 bound, still only advisory
    pts = [p for p in rep.flagged_points if p.metric_id == "feather_band.de00_p99"]
    assert 1 <= len(pts) <= gating.TILES_PER_METRIC
    h, w = room_a["plate"].shape[:2]
    for p in pts:
        assert _inside(p, w, h) and p.w <= gating.TILE_PX and p.h <= gating.TILE_PX
        assert "dE00" in p.note
        # the band hugs the edit rectangle (x 300..600, y 40..200): the tile reaches it
        assert p.x < 600 and p.x + p.w > 300 and p.y < 200 and p.y + p.h > 40
    means = [float(p.note.split()[2]) for p in pts]
    assert means == sorted(means, reverse=True) and means[0] > 1.0


@needs_room_a
def test_a_leak_outside_the_mask_is_flagged_where_it_happened(room_a):
    mask = _box_mask(room_a)
    out = room_a["plate"].copy()
    out[140:152, 20:40] = np.clip(out[140:152, 20:40].astype(np.int64) + 12000, 0, 65535).astype(np.uint16)
    rep = _run(room_a, out, mask=mask)
    by = {c.metric_id: c for c in rep.checks}
    assert by["outside_mask.de00"].status == "fail" and rep.verdict == "fail"
    pts = [p for p in rep.flagged_points if p.metric_id == "outside_mask.de00"]
    assert len(pts) == 1  # the leak sits inside one tile
    p = pts[0]
    assert (p.x, p.y) == (0, 128) and p.x <= 20 and 40 <= p.x + p.w and p.y <= 140 and 152 <= p.y + p.h
    assert "outside the mask" in p.note


@needs_room_a
def test_a_locked_material_that_drifts_is_flagged_on_its_pixels(room_a):
    ranks, cov = room_a["material"]
    from plate_gates import material_de

    wall = np.uint32(material_de.id_to_u32("m:57b3d007"))  # ARCH_wall_N, 27 % of the frame
    wall_px = np.where(ranks == wall, cov, 0.0).sum(axis=2) >= material_de.MASK_THRESHOLD
    cols = np.nonzero(wall_px.any(axis=0))[0]
    cut = cols[int(len(cols) * 0.35)]  # the left third of the wall: under half of its pixels
    mask = wall_px & (np.arange(wall_px.shape[1])[None, :] <= cut)
    assert 0.1 < mask.sum() / wall_px.sum() < 0.5
    rep = _run(room_a, _brighten(room_a["plate"], mask, 9000), mask=mask)
    by = {c.metric_id: c for c in rep.checks}
    assert by["material.de00_p90"].status == "fail" and by["outside_mask.de00"].status == "pass"
    pts = [p for p in rep.flagged_points if p.metric_id == "material.de00_p90"]
    assert 1 <= len(pts) <= gating.TILES_PER_METRIC
    for p in pts:
        assert wall_px[p.y : p.y + p.h, p.x : p.x + p.w].any()  # the tile holds pixels of the locked material
        assert "locked materials" in p.note


# ---------------------------------------------------------------------------- invention flags and tier A skips


@pytest.fixture(scope="module")
def window():
    ref, test, mask, exp = make_pair("window_added", (256, 256))
    scene = exp["scene"]
    edges = gbuffer.structural_edges(*scene[:4], EdgeParams(width=None))
    camera = {"dof_enabled": False, "focal_length_mm": 35.0, "film_width_mm": 36.0}
    ids = scene.ids
    return {
        "ref": ref,
        "test": test,
        "exp": exp,
        "edges": edges,
        "scene": scene,
        "camera": camera,
        "crypto": (ids[..., None].astype(np.uint32), np.ones(ids.shape + (1,), dtype=np.float32)),
    }


def _legend(exp, *, planar=True):
    return {
        "entries": [
            {
                "id": exp["wall_id"],
                "kind": "object",
                "name": "ARCH_wall_N",
                "class": "architecture",
                "planar": planar,
            },
            {
                "id": exp["box_id"],
                "kind": "object",
                "name": "ARCH_panel_box",
                "class": "architecture",
                "planar": planar,
            },
        ]
    }


def _run_window(win, legend, out=None, depth_fn=None):
    ctx = {
        "z": win["scene"][0],
        "manifest": {"camera": win["camera"]},
        "ids": win["scene"].ids,
        "legend": legend,
    }
    return gating.run_gates(
        win["ref"],
        win["test"] if out is None else out,
        {"path": ROOT, "manifest": {"tier": "A"}},
        legend,
        "A",
        {"id": "synthetic"},
        registration=REG_OK,
        edges=(win["edges"], ctx),
        crypto_material=win["crypto"],
        depth_fn=depth_fn,
    )


def test_a_window_added_to_the_wall_is_flagged_where_it_was_painted(window):
    exp = window["exp"]
    rep = _run_window(window, _legend(exp))
    assert rep.metrics["invention_flags"] == exp["n_invention_flags"] == 1
    pts = [p for p in rep.flagged_points if p.metric_id == invention.METRIC_ID]
    assert len(pts) == 1
    p = pts[0]
    x0, y0, x1, y1 = exp["rect"]
    assert p.x >= x0 - 3 and p.y >= y0 - 3 and p.x + p.w <= x1 + 3 and p.y + p.h <= y1 + 3
    assert p.x <= x0 + 1 and p.y <= y0 + 1 and p.x + p.w >= x1 - 1 and p.y + p.h >= y1 - 1
    assert "ARCH_wall_N" in p.note and _inside(p, 256, 256)
    assert {c.metric_id: c.status for c in rep.checks}["invention_flags"] == "fail"
    assert rep.model_dump(mode="json")["flagged_points"][0]["metric_id"] == "invention_flags"


def test_a_clean_render_flags_nothing(window):
    rep = _run_window(window, _legend(window["exp"]), out=window["ref"])
    assert rep.metrics["invention_flags"] == 0 and rep.flagged_points == []


def test_a_tier_a_legend_without_planar_ids_is_skipped_with_its_own_reason(window):
    rep = _run_window(window, _legend(window["exp"], planar=False))
    assert rep.metrics["invention"]["reason"] == "tier A: legend marks no planar ids"
    assert rep.metrics["invention"]["skipped"] is True and rep.metrics["invention_flags"] is None
    assert "invention: skipped (tier A: legend marks no planar ids)" in rep.backend_notes
    assert {c.metric_id: c.status for c in rep.checks}["invention_flags"] == "advisory"
    assert not [p for p in rep.flagged_points if p.metric_id == invention.METRIC_ID]


def test_invention_boxes_are_scaled_from_the_working_size_to_the_plate():
    inv = {
        "flags": [{"bbox": [10, 20, 31, 41], "length_px": 50, "name": "wall", "id": "o:1", "class": "ground"}]
    }
    (p,) = gating._invention_points(inv, (256, 128), 512, 256)  # the edge map is half the plate
    assert (p.x, p.y, p.w, p.h) == (20, 40, 42, 42) and p.metric_id == "invention_flags"
    (q,) = gating._invention_points(inv, (256, 128), 256, 128)
    assert (q.x, q.y, q.w, q.h) == (10, 20, 21, 21)
    edge = {"flags": [{"bbox": [250, 120, 256, 128], "length_px": 9, "id": "o:2", "class": "ground"}]}
    (e,) = gating._invention_points(edge, (256, 128), 300, 150)
    assert e.x + e.w <= 300 and e.y + e.h <= 150 and "o:2" in e.note


# ---------------------------------------------------------------------------- tiles, camera drift, depth note


def test_worst_tiles_rank_by_mean_and_clamp_to_the_frame():
    h, w = 300, 400  # tiles of 128: the last column is 16 px wide, the last row 44 px high
    de = np.zeros((h, w), dtype=np.float32)
    region = np.ones((h, w), dtype=bool)
    de[0:128, 0:128] = 1.0
    de[128:256, 128:256] = 3.0
    de[256:300, 384:400] = 9.0  # the clamped corner tile
    tiles = gating._worst_tiles(de, region)
    assert [t[:4] for t in tiles] == [(384, 256, 16, 44), (128, 128, 128, 128), (0, 0, 128, 128)]
    assert [round(t[4], 6) for t in tiles] == [9.0, 3.0, 1.0]
    assert gating._worst_tiles(de, region, n=1)[0][:4] == (384, 256, 16, 44)
    # a tile with no region pixel, or with a zero mean, is never named
    half = np.zeros((h, w), dtype=bool)
    half[:, :128] = True
    assert [t[:4] for t in gating._worst_tiles(de, half)] == [(0, 0, 128, 128)]
    assert gating._worst_tiles(np.zeros((h, w), dtype=np.float32), region) == []


def test_a_failed_registration_shift_flags_the_image_centre():
    rng = np.random.default_rng(3)
    plate = rng.integers(0, 65535, (300, 400, 3)).astype(np.uint16)
    drift = {"shift_px": 12.0, "residual_px": 0.2, "registered": True, "camera_drift": True}
    rep = gating.run_gates(plate, plate.copy(), None, None, "C", {"id": "fake"}, registration=drift)
    assert {c.metric_id: c.status for c in rep.checks}["registration.shift_px"] == "fail"
    (p,) = [p for p in rep.flagged_points if p.metric_id == "registration.shift_px"]
    assert p.note == "camera drift" and (p.w, p.h) == (128, 128)
    assert (p.x + p.w / 2, p.y + p.h / 2) == (200, 150)
    ok = gating.run_gates(plate, plate.copy(), None, None, "C", {"id": "fake"}, registration=REG_OK)
    assert ok.flagged_points == []
    # an unmeasured shift fails the gate but is not "camera drift"
    unmeasured = gating.run_gates(plate, plate.copy(), None, None, "C", None, registration={"shift_px": None})
    assert not [p for p in unmeasured.flagged_points if p.metric_id == "registration.shift_px"]


def test_the_depth_backend_is_named_in_the_notes():
    rng = np.random.default_rng(4)
    plate = rng.integers(0, 65535, (96, 128, 3)).astype(np.uint16)
    z = np.tile(np.linspace(1.0, 5.0, 128, dtype=np.float32), (96, 1))

    def named(img):
        return z

    named.name = "midas-stub"
    rep = gating.run_gates(plate, plate.copy(), None, None, "C", None, registration=REG_OK, depth_fn=named)
    assert "depth agreement backend: midas-stub" in rep.backend_notes
    anon = gating.run_gates(
        plate, plate.copy(), None, None, "C", None, registration=REG_OK, depth_fn=lambda img: z
    )
    assert "depth agreement backend: unknown" in anon.backend_notes
    none = gating.run_gates(plate, plate.copy(), None, None, "C", None, registration=REG_OK)
    assert not any(n.startswith("depth agreement backend") for n in none.backend_notes)


def test_a_depth_backend_on_a_tier_a_run_is_named_but_not_scored(window):
    rep = _run_window(window, _legend(window["exp"]), depth_fn=lambda img: np.ones(img.shape[:2], np.float32))
    assert "depth agreement backend: unknown" in rep.backend_notes
    assert "depth_agreement" not in rep.metrics
    assert any("not scored on tier A" in n for n in rep.backend_notes)


def test_flagged_points_are_json_plain():
    rng = np.random.default_rng(5)
    plate = rng.integers(0, 65535, (96, 128, 3)).astype(np.uint16)
    mask = np.zeros((96, 128), dtype=bool)
    mask[20:70, 30:100] = True
    out = _brighten(plate, mask, 6000)
    rep = gating.run_gates(plate, out, None, None, "C", None, mask=mask, registration=REG_OK)
    data = json.loads(rep.to_json())
    assert all(set(p) == {"x", "y", "w", "h", "metric_id", "note"} for p in data["flagged_points"])
    assert data["flagged_points"] and all(isinstance(p["x"], int) for p in data["flagged_points"])
    assert all(math.isfinite(float(p["note"].split()[2])) for p in data["flagged_points"])
