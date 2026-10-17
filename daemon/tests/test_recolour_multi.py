"""Recolour with several materials in one pass (router plan T90).

`params.materials: [{material, target}, ...]` runs `recolour.apply` per entry and lays the deltas of all of them
on the display master: two materials in one run equal two single recolours laid on one plate (within one LSB of
the 16-bit output, which is the rounding of summing before versus after). Everything is checked on the committed
Blender packet `room_a` (an ARCH panel at RAL 7016 and the north wall, both with raw lighting, GI and reflection
passes). The one-entry list and the old `material` + `target` pair are the same request, and the pair's pixels are
checked against the formula the node used before T90 so the single-material path cannot drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.nodes import install
from plate_daemon.nodes import recolour_node as rn
from plate_daemon.nodes import routes as node_routes
from plate_daemon.nodes.common import RunContext, material_share, to_display_png16
from plate_daemon.pipeline import PipelineError, decode_png16

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
PANEL = "m:735a8655"  # ARCH_panel_7016 (locked by the packet policy)
WALL = "m:57b3d007"  # ARCH_wall_N (locked)
PEOPLE = "m:ebb52806"  # PEOPLE_dummy (editable)
FLOOR = "m:0b411b0a"  # HARD_floor

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets not built yet (T2)",
)


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    pipeline: Any
    runner: Any
    packet_id: str = ""

    def start(self) -> None:
        """project + room_a ingested + graph `Plate -> Recolour` (node id `n`, nothing saved on it)."""
        r = self.client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=self.auth
        )
        assert r.status_code == 201, r.text
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**self.auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        self.packet_id = r.json()["packet_id"]
        self.start_graph({})

    def start_graph(self, params: dict[str, Any]) -> None:
        """Replace the graph with `Plate -> Recolour` whose node saves `params`."""
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": self.packet_id, "tier": "A", "label": "Plate"},
                {"id": "n", "kind": "Recolour", "label": "Recolour", "params": params},
            ],
            "edges": [{"id": "p->n", "source": "p", "target": "n"}],
        }
        r = self.client.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=self.auth)
        assert r.status_code == 200, r.text

    @property
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def make(self, params: dict[str, Any]):
        with self.pipeline.index.project(PROJECT) as conn:
            return self.runner.make(conn, PROJECT, "n", {"params": params})

    def png(self, key: str) -> np.ndarray:
        return decode_png16(self.cas.read_bytes(key))

    def report(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/v0/runs/{run_id}/gate", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def master(self) -> np.ndarray:
        return self.pipeline.packet(PROJECT, self.packet_id).plate16()


@pytest.fixture
def env(plate_env, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        runner = install(pipeline)
        client.app.include_router(node_routes.router)
        e = Env(client, {"Authorization": f"Bearer {settings.token}"}, pipeline, runner)
        e.start()
        yield e


def _check(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return next(c for c in report["checks"] if c["metric_id"] == metric_id)


def _legacy_png(env: Env, material: str, target: str, method: str | None = None) -> np.ndarray:
    """One material the way the node did it before T90, from the primitives: apply, display, delta on the master."""
    from plate_gates import recolour
    from plate_pixels import exr

    pv = env.pipeline.packet(PROJECT, env.packet_id)
    present = [e["element"] for e in pv.files["beauty_exr"]["elements"]]
    elements = exr.read_elements(
        pv.root / pv.files["beauty_exr"]["path"], pv.manifest, [e for e in rn.ELEMENTS if e in present]
    )
    share = material_share(pv, material)
    res = recolour.apply(
        elements,
        share,
        rn.parse_target(target)["lab"],
        rn.legend_entry(pv.legend, material),
        method=method,
        K=pv.manifest["camera"],
    )
    pipeline = env.runner.colour(pv.manifest)
    disp_new = pipeline.rederive_display(res["rgb_linear"])
    disp_old = pipeline.rederive_display(np.asarray(elements["beauty"][..., :3], dtype=np.float32))
    master16 = pv.plate16()
    master8 = master16.astype(np.float32) / 257.0
    sel = share > 0.0
    out16 = master16.copy()
    out16[sel] = to_display_png16(master8[sel] + (disp_new[sel] - disp_old[sel]))
    return out16


# ---------------------------------------------------------------------------- the headline: one pass = two passes


def test_two_materials_in_one_run_equal_two_single_recolours(env):
    a = env.make({"material": PANEL, "target": "RAL 3020"})
    b = env.make({"material": WALL, "target": "#CBD0CC"})
    both = env.make(
        {"materials": [{"material": PANEL, "target": "RAL 3020"}, {"material": WALL, "target": "#CBD0CC"}]}
    )
    assert (a.state.value, b.state.value, both.state.value) == ("DONE", "DONE", "DONE"), both.error

    master = env.master().astype(np.int64)
    out_a, out_b, out_ab = (env.png(r.outputs[0]).astype(np.int64) for r in (a, b, both))
    assert (out_a != master).any() and (out_b != master).any()  # each single run moved something
    assert (out_ab != out_a).any() and (out_ab != out_b).any()  # and the pair is neither of them
    # master + (A - master) + (B - master): the two recolours laid on one plate, within one 16-bit LSB. RAL 3020
    # clips red at white next to the wall, so this also holds where one of the two single outputs saturates.
    assert np.abs(out_ab - (out_a + out_b - master)).max() <= 1

    # The coverages of distinct materials do not overlap, so outside both the output is the plate bit for bit.
    pv = env.pipeline.packet(PROJECT, env.packet_id)
    union = (material_share(pv, PANEL) > 0) | (material_share(pv, WALL) > 0)
    assert np.array_equal(out_ab[~union], master[~union])

    # normalized.materials: {material, target, method, expected_de00}, in request order.
    assert [m["material"] for m in both.normalized["materials"]] == [PANEL, WALL]
    for m in both.normalized["materials"]:
        assert m.keys() == {"material", "target", "method", "expected_de00"}
        assert m["method"] == "recomposition" and m["expected_de00"] <= 1.0
    assert both.normalized["materials"][0]["target"]["label"] == "RAL 3020"
    assert both.normalized["materials"][1]["target"]["label"] == "#CBD0CC"
    assert both.normalized["mask_px"] == int(union.sum())
    assert both.state.value == "DONE" and both.gate_report["verdict"] == "pass"

    # The gate scores every material and the outside of the union (hard).
    rep = env.report(both.id)
    assert rep["verdict"] == "pass"
    per = rep["metrics"]["per_material"]
    assert list(per) == [PANEL, WALL] and all(r["exact"] and r["mask_px"] > 1000 for r in per.values())
    assert _check(rep, "recolour.mask_px")["status"] == "pass"
    assert _check(rep, "outside_mask.changed_px")["value"] == 0.0
    assert _check(rep, "recolour.expected_de00")["status"] == "pass"
    de = _check(rep, "recolour.de00_median")  # the registered bound, for the worst material's method
    assert de["status"] == "pass" and de["op"] == "le" and de["threshold"] == 1.0
    assert rep["metrics"]["outside_mask"] == {"identical_outside": True, "changed_px": 0}
    assert sum(1 for n in rep["backend_notes"] if "locked" in n) == 2  # both panels' locks are said, by id
    assert any(PANEL in n for n in rep["backend_notes"]) and any(WALL in n for n in rep["backend_notes"])


def test_the_worst_material_decides_the_verdict(env):
    run = env.make(
        {
            "materials": [
                {"material": PANEL, "target": "RAL 3020"},
                {"material": WALL, "target": "#CBD0CC", "method": "degraded"},
            ]
        }
    )
    assert run.state.value == "DONE", run.error  # advisory is not a failure
    rep = env.report(run.id)
    assert rep["verdict"] == "advisory"
    per = rep["metrics"]["per_material"]
    assert (per[PANEL]["method"], per[PANEL]["expected_de00"], per[PANEL]["exact"]) == (
        "recomposition",
        1.0,
        True,
    )
    assert (per[WALL]["method"], per[WALL]["expected_de00"], per[WALL]["exact"]) == ("degraded", 4.0, False)
    expected = _check(rep, "recolour.expected_de00")
    assert expected["value"] == 4.0 and expected["status"] == "advisory"  # worst wins
    median = _check(rep, "recolour.de00_median")
    assert median["value"] == 4.0 and median["status"] == "advisory" and median["threshold"] == 4.0
    assert rep["metrics"]["recolour"]["method"] == "degraded" and rep["metrics"]["material"] == WALL
    assert _check(rep, "outside_mask.changed_px")["status"] == "pass"  # the outside stays hard and clean
    # The siblings' ranking number follows the worst material too.
    assert run.gate_score == pytest.approx(-4.0)


def test_a_top_level_method_is_the_default_of_entries_that_name_none(env):
    url = f"/api/v0/projects/{PROJECT}/nodes/n/preview"
    body = {
        "what": "recolour",
        "params": {
            "method": "ratio",
            "materials": [
                {"material": PANEL, "target": "RAL 3020"},
                {"material": WALL, "target": "#CBD0CC", "method": "recomposition"},
            ],
        },
    }
    r = env.client.post(url, json=body, headers=env.auth)
    assert r.status_code == 200, r.text
    rows = r.json()["materials"]
    assert [(m["material"], m["method"]) for m in rows] == [(PANEL, "ratio"), (WALL, "recomposition")]


# ---------------------------------------------------------------------------- the old pair is a one-entry list


def test_the_one_material_form_is_unchanged_and_equals_a_one_entry_list(env):
    pair = env.make({"material": PANEL, "target": "RAL 3020"})
    listed = env.make({"materials": [{"material": PANEL, "target": "RAL 3020"}]})
    assert (pair.state.value, listed.state.value) == ("DONE", "DONE")
    assert pair.outputs == listed.outputs  # the same bytes, stored once
    # ...and they are the pixels the node made before T90.
    assert np.array_equal(env.png(pair.outputs[0]), _legacy_png(env, PANEL, "RAL 3020"))
    # The legacy keys are still there, now the worst (here the only) material's.
    info = pair.normalized["recolour"]
    assert info["method"] == "recomposition" and info["expected_de00"] <= 1.0 and info["n_px"] > 1000
    assert pair.normalized["materials"][0]["material"] == PANEL
    rep = env.report(pair.id)
    assert rep["metrics"]["material"] == PANEL and rep["metrics"]["target"]["label"] == "RAL 3020"
    assert rep["metrics"]["moved_de00_median"] is not None
    assert list(rep["metrics"]["per_material"]) == [PANEL]


def test_a_saved_materials_list_runs_and_a_request_pair_overrides_it(env):
    saved = [{"material": PANEL, "target": "RAL 3020"}, {"material": WALL, "target": "#CBD0CC"}]
    env.start_graph({"materials": saved})
    run = env.make({})
    assert [m["material"] for m in run.normalized["materials"]] == [PANEL, WALL]
    # The artist's request wins over what the node saved: a single pair is one material.
    one = env.make({"material": FLOOR, "target": "#808080"})
    assert [m["material"] for m in one.normalized["materials"]] == [FLOOR]


def test_a_run_quoted_before_t90_still_normalizes_and_gates(env):
    """A spec holding one `material` + `target` (a run left in flight across an upgrade) reads as one entry."""
    new = env.make({"material": PANEL, "target": "RAL 3020"})
    spec = dict(new.inputs["numeric"])
    entry = spec.pop("materials")[0]
    legacy = {
        **spec,
        "material": entry["material"],
        "target": entry["target"],
        "source_lab": None,
        "method": None,
    }
    assert rn.spec_entries(legacy) == [entry]

    c = RunContext(env.runner, env.cas, PROJECT, legacy, env.packet_id)
    norm = rn.normalize(c)
    assert env.png(norm["outputs"][0]).tolist() == env.png(new.outputs[0]).tolist()

    class OldNormalized(RunContext):
        @property
        def normalized(
            self,
        ) -> dict[str, Any]:  # what a pre-T90 run stored: one summary, no per-material list
            return {k: v for k, v in norm.items() if k not in ("recolours", "materials")}

    old = OldNormalized(env.runner, env.cas, PROJECT, legacy, env.packet_id, new)
    report = rn.gate(old)
    assert report.verdict == "pass" and list(report.metrics["per_material"]) == [PANEL]
    assert report.metrics["recolour"]["method"] == "recomposition"


# ---------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("params", "code"),
    [
        # a duplicate id, however it is written
        (
            {
                "materials": [
                    {"material": PANEL, "target": "RAL 3020"},
                    {"material": PANEL, "target": "#CBD0CC"},
                ]
            },
            "bad_params",
        ),
        (
            {
                "materials": [
                    {"material": PANEL, "target": "RAL 3020"},
                    {"material": PANEL.upper().replace("M:", "m:"), "target": "RAL 7016"},
                ]
            },
            "bad_params",
        ),
        # 1 to 8 entries
        ({"materials": []}, "bad_params"),
        ({"materials": [{"material": f"m:{i:08x}", "target": "RAL 3020"} for i in range(9)]}, "bad_params"),
        # shapes
        ({"materials": {"material": PANEL, "target": "RAL 3020"}}, "bad_params"),
        ({"materials": [PANEL]}, "bad_params"),
        ({"materials": [{"target": "RAL 3020"}]}, "bad_params"),
        ({"materials": [{"material": "ARCH_panel", "target": "RAL 3020"}]}, "bad_params"),
        ({"materials": [{"material": PANEL}]}, "bad_params"),
        ({"materials": [{"material": PANEL, "target": "RAL 3020", "method": "magic"}]}, "bad_params"),
        # a later entry is refused like a lone one
        (
            {
                "materials": [
                    {"material": PANEL, "target": "RAL 3020"},
                    {"material": "m:deadbeef", "target": "RAL 3020"},
                ]
            },
            "unknown_id",
        ),
        (
            {"materials": [{"material": PANEL, "target": "RAL 3020"}, {"material": WALL, "target": "puce"}]},
            "bad_target",
        ),
        (
            {
                "materials": [
                    {"material": PANEL, "target": "RAL 3020"},
                    {"material": WALL, "target": "RAL 9999"},
                ]
            },
            "unknown_ral",
        ),
    ],
)
def test_bad_materials_are_refused_before_any_run(env, params, code):
    with pytest.raises(PipelineError) as e:
        env.make(params)
    assert e.value.code == code and e.value.status == 422, (params, e.value)
    with env.pipeline.index.project(PROJECT) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # refused at the quote: no run


def test_a_duplicate_id_is_a_422_bad_params_over_http(env):
    url = f"/api/v0/projects/{PROJECT}/nodes/n/quote"
    dup = {
        "materials": [{"material": PANEL, "target": "RAL 3020"}, {"material": PANEL, "target": "RAL 7016"}]
    }
    r = env.client.post(url, json={"params": dup}, headers=env.auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_params", r.text
    assert PANEL in r.json().get("message", r.text)
    ok = env.client.post(
        url,
        json={
            "params": {
                "materials": [
                    {"material": PANEL, "target": "RAL 3020"},
                    {"material": WALL, "target": "RAL 7016"},
                ]
            }
        },
        headers=env.auth,
    )
    assert ok.status_code == 200, ok.text


def test_eight_materials_is_the_most_and_all_of_them_are_exact(env):
    ids = [FLOOR, "m:5263bdbf", WALL, "m:b42836e4", PANEL, "m:b844e457", PEOPLE, "m:929c9e95"]
    targets = ["#383E42", "RAL 3020", "#CBD0CC", "RAL 7016", "RAL 3020", "#2E7D32", "#C1121C", "#F1ECE1"]
    run = env.make({"materials": [{"material": m, "target": t} for m, t in zip(ids, targets, strict=True)]})
    assert run.state.value == "DONE", run.error
    assert [m["material"] for m in run.normalized["materials"]] == ids
    per = env.report(run.id)["metrics"]["per_material"]
    assert len(per) == 8 and all(r["mask_px"] > 0 and r["exact"] for r in per.values())
    out = env.png(run.outputs[0]).astype(np.int64)
    # Each material alone, laid on one plate, is what the one pass made.
    master = env.master().astype(np.int64)
    singles = master.copy()
    for m, t in zip(ids, targets, strict=True):
        singles += _legacy_png(env, m, t).astype(np.int64) - master
    singles = np.clip(singles, 0, 65535)
    assert np.array_equal(out, singles)


# ---------------------------------------------------------------------------- preview and helpers


def test_preview_gives_the_expected_error_per_entry(env):
    url = f"/api/v0/projects/{PROJECT}/nodes/n/preview"
    params = {
        "materials": [
            {"material": PANEL, "target": "RAL 3020"},
            {"material": WALL, "target": {"lab": [60.0, 0.0, 0.0]}, "method": "degraded"},
        ]
    }
    r = env.client.post(url, json={"what": "recolour", "params": params}, headers=env.auth)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["materials"]
    assert [(m["material"], m["method"], m["expected_de00"]) for m in rows] == [
        (PANEL, "recomposition", 1.0),
        (WALL, "degraded", 4.0),
    ]
    assert rows[0]["target"]["kind"] == "ral" and rows[1]["target"]["kind"] == "lab"
    assert all(m["n_px"] > 1000 and m["metal"] is False for m in rows)
    # The headline is the worst material's, so a one-material reader still sees the number that matters.
    assert (body["material"], body["method"], body["expected_de00"]) == (WALL, "degraded", 4.0)
    # A one-material preview keeps its old shape.
    r = env.client.post(
        url, json={"what": "recolour", "params": {"material": PANEL, "ral": "3020"}}, headers=env.auth
    )
    assert r.status_code == 200 and r.json()["method"] == "recomposition" and r.json()["expected_de00"] == 1.0
    assert [m["material"] for m in r.json()["materials"]] == [PANEL]
    # A refusal in any entry is the preview's refusal.
    bad = {
        "materials": [{"material": PANEL, "target": "RAL 3020"}, {"material": PANEL, "target": "RAL 3020"}]
    }
    r = env.client.post(url, json={"what": "recolour", "params": bad}, headers=env.auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_params"


def test_worst_index_prefers_a_miss_then_the_larger_error_then_the_first():
    exact = {"method": "recomposition", "expected_de00": 1.0, "metal": False}
    metal = {"method": "recomposition", "expected_de00": 1.0, "metal": True}
    ratio = {"method": "ratio", "expected_de00": 2.5, "metal": False}
    degraded = {"method": "degraded", "expected_de00": 4.0, "metal": False}
    assert rn.worst_index([exact]) == 0
    assert rn.worst_index([exact, exact]) == 0  # the first of equals
    assert rn.worst_index([exact, ratio, degraded]) == 2
    assert rn.worst_index([degraded, ratio, exact]) == 0
    assert rn.worst_index([exact, metal]) == 1  # a metal misses the bound at the same number
    assert rn.worst_index([metal, ratio]) == 1
    assert rn.is_exact(exact) and not rn.is_exact(metal) and not rn.is_exact(ratio)
