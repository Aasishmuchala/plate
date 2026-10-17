"""invention.py: new long edges inside planar single-id regions (router plan T15).

Synthetic vectors: the window_added pair (a flat rectangle painted on the wall, away from every geometric
edge) and its along_edge variant (the box's front face repainted: its outline is existing geometry). The
room_a tests read the committed Blender fixture packet and skip when it is not built yet (T2).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import pytest
from fixtures.synthetic import make_pair

from plate_gates import gbuffer, invention, thresholds
from plate_gates import io as gio
from plate_gates.gbuffer import EdgeParams

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"


def _legend(exp, *, wall_class="architecture", planar=True):
    return {
        "entries": [
            {
                "id": exp["wall_id"],
                "kind": "object",
                "name": "ARCH_wall_N",
                "class": wall_class,
                "class_source": "rule",
                "pixel_share": 0.5,
                "planar": planar,
            },
            {
                "id": exp["box_id"],
                "kind": "object",
                "name": "ARCH_panel_box",
                "class": "architecture",
                "class_source": "rule",
                "pixel_share": 0.1,
                "planar": True,
            },
        ]
    }


@pytest.fixture(scope="module")
def window():
    ref, test, mask, exp = make_pair("window_added", (256, 256))
    scene = exp["scene"]
    edges = gbuffer.structural_edges(*scene[:4], EdgeParams(width=None))
    return ref, test, mask, exp, scene, edges


@pytest.fixture(scope="module")
def room_a():
    if not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    emap, ctx = gbuffer.from_packet(ROOM_A)
    display = gio.to_uint8(gio.load_rgb(ROOM_A / "display.tif"))
    return emap, ctx, display


def test_window_added_is_exactly_one_flag_on_the_wall(window):
    ref, test, mask, exp, scene, edges = window
    found = invention.flags(test, edges, scene.ids, _legend(exp))
    assert found is not None and len(found) == exp["n_invention_flags"] == 1
    f = found[0]
    assert f.id == exp["wall_id"] and f.class_ == "architecture" and f.name == "ARCH_wall_N"
    x0, y0, x1, y1 = exp["rect"]
    bx0, by0, bx1, by1 = f.bbox
    assert bx0 <= x0 + 1 and by0 <= y0 + 1 and bx1 >= x1 - 1 and by1 >= y1 - 1  # bbox contains the rectangle
    assert bx0 >= x0 - 3 and by0 >= y0 - 3 and bx1 <= x1 + 3 and by1 <= y1 + 3  # and is the rectangle
    assert f.length_px >= 2 * ((x1 - x0) + (y1 - y0)) * 0.8  # most of the outline
    assert f.n_edges >= 1
    d = f.to_dict()
    assert set(d) == {"id", "class", "name", "bbox", "length_px", "n_edges", "centroid"}
    json.dumps(d)
    # the same with the plate given: the plate has no edge there, so the flag stays
    assert len(invention.flags(test, edges, scene.ids, _legend(exp), plate_rgb=ref)) == 1


def test_window_along_an_existing_edge_is_not_an_invention():
    ref, test, mask, exp = make_pair("window_added", (256, 256), along_edge=True)
    scene = exp["scene"]
    edges = gbuffer.structural_edges(*scene[:4], EdgeParams(width=None))
    assert exp["n_invention_flags"] == 0
    assert invention.flags(test, edges, scene.ids, _legend(exp)) == []
    assert invention.flags(ref, edges, scene.ids, _legend(exp)) == []  # the unchanged render


def test_report_shape_and_verdict(window):
    ref, test, mask, exp, scene, edges = window
    rep = invention.report(test, edges, scene.ids, _legend(exp))
    assert rep["metric_id"] == "invention_flags"
    assert rep["count"] == 1 and rep["status"] == "fail" and rep["bound"] == 0.0 and rep["skipped"] is False
    assert rep["reason"] is None and len(rep["flags"]) == 1 and rep["flags"][0]["class"] == "architecture"
    assert rep["params"]["min_len"] == thresholds.long_edge_min_px(256) == 5
    assert rep["params"]["width"] == 256 and rep["params"]["plate_subtracted"] is False
    assert rep["params"]["scanned_ids"] == [exp["wall_id"], exp["box_id"]]
    json.dumps(rep)
    clean = invention.report(ref, edges, scene.ids, _legend(exp))
    assert clean["count"] == 0 and clean["status"] == "pass"
    assert invention.verdict(0) == ("pass", 0.0) and invention.verdict(3)[0] == "fail"
    assert invention.verdict(None) == ("advisory", 0.0)


def test_tier_c_and_legends_without_planar_ids_skip(window):
    ref, test, mask, exp, scene, edges = window
    assert invention.flags(test, None, None, None) is None
    assert invention.flags(test, edges, scene.ids, _legend(exp), tier="C") is None
    assert invention.flags(test, edges, None, _legend(exp)) is None
    assert invention.flags(test, edges, scene.ids, {"entries": []}) is None
    assert (
        invention.flags(test, edges, scene.ids, _legend(exp, planar=False)) == []
    )  # the box is still scanned
    only_wall = {"entries": _legend(exp)["entries"][:1]}
    assert (
        invention.flags(test, edges, scene.ids, {"entries": [{**only_wall["entries"][0], "planar": False}]})
        is None
    )
    rep = invention.report(test, edges, scene.ids, _legend(exp), tier="C")
    assert rep["skipped"] is True and rep["count"] is None and rep["reason"] == invention.TIER_C_REASON
    assert rep["status"] == "advisory" and rep["flags"] == [] and rep["params"]["scanned_ids"] == []


def test_only_architecture_ground_and_glazing_are_scanned(window):
    ref, test, mask, exp, scene, edges = window
    veg = _legend(exp, wall_class="vegetation")
    found = invention.flags(test, edges, scene.ids, veg)
    assert found == []  # the box (architecture) is scanned and clean; the vegetation wall is not scanned
    assert [e["id"] for e in invention.planar_entries(veg)] == [exp["box_id"]]
    for cls in invention.PLANAR_CLASSES:
        assert len(invention.flags(test, edges, scene.ids, _legend(exp, wall_class=cls))) == 1
    assert invention.PLANAR_CLASSES == ("architecture", "ground", "glazing")


def test_the_long_edge_cut_is_applied_to_component_length(window):
    """A 30 px line on the wall is one flag; the cut is exactly `min_len` on the component's pixel count
    (Canny outlines a 1 px line on both sides, so the component is about twice the drawn length)."""
    ref, test, mask, exp, scene, edges = window
    line = ref.copy()
    cv2.line(line, (40, 60), (70, 60), (20, 20, 20), 1)
    found = invention.flags(line, edges, scene.ids, _legend(exp))
    assert len(found) == 1 and found[0].length_px >= 30
    n = found[0].length_px
    keep = invention.InventionParams(min_len=n)
    drop = invention.InventionParams(min_len=n + 1)
    assert len(invention.flags(line, edges, scene.ids, _legend(exp), params=keep)) == 1
    assert invention.flags(line, edges, scene.ids, _legend(exp), params=drop) == []
    # the default cut scales with the working width: 5 px at 256, 13 at 640, 41 at 2048
    assert thresholds.long_edge_min_px(256) == 5
    assert thresholds.long_edge_min_px(640) == 13
    assert thresholds.long_edge_min_px(2048) == 41


def test_room_a_display_has_no_inventions_against_itself(room_a):
    """A planar id is not a flat colour: floor tiles, a shadow line and clouds through the pane are long
    edges the render already shows (7 flags against the G-buffer alone). With the plate subtracted: 0."""
    emap, ctx, display = room_a
    alone = invention.flags(display, emap, ctx["ids"], ctx["legend"])
    assert alone is not None and len(alone) > 0
    assert invention.flags(display, emap, ctx["ids"], ctx["legend"], plate_rgb=display) == []
    rep = invention.report(display, emap, ctx["ids"], ctx["legend"], plate_rgb=display)
    assert rep["count"] == 0 and rep["status"] == "pass" and rep["params"]["plate_subtracted"] is True
    assert rep["params"]["min_len"] == thresholds.long_edge_min_px(640) == 13
    scanned = set(rep["params"]["scanned_ids"])
    assert {"o:57b3d007", "o:0b411b0a", "o:b42836e4"} <= scanned  # ARCH_wall_N, HARD_floor, GLZ_pane
    assert "o:55cbe981" not in scanned  # VEG_shrub_A


def test_room_a_rectangle_drawn_on_the_wall_is_one_flag(room_a):
    emap, ctx, display = room_a
    painted = display.copy()
    cv2.rectangle(painted, (440, 40), (500, 100), (40, 40, 40), 2)
    found = invention.flags(painted, emap, ctx["ids"], ctx["legend"], plate_rgb=display)
    assert len(found) == 1
    f = found[0]
    assert f.id == "o:57b3d007" and f.name == "ARCH_wall_N" and f.class_ == "architecture"
    x0, y0, x1, y1 = f.bbox
    assert x0 <= 440 and y0 <= 40 and x1 >= 500 and y1 >= 100
    assert x0 >= 434 and y0 >= 34 and x1 <= 507 and y1 <= 107
    assert f.length_px >= 200
    assert invention.report(painted, emap, ctx["ids"], ctx["legend"], plate_rgb=display)["status"] == "fail"
