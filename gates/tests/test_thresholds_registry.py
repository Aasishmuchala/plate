"""The threshold registry (router plan T80): numbers that lived as module constants are registered in
thresholds.yaml under a new set id, the old set stays readable, and the two cannot drift apart."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from plate_gates import (
    calibration,
    depth_agreement,
    recolour,
    router_math,
    setgate,
    threshold_sets,
    thresholds,
)

REPO = Path(__file__).resolve().parents[2]
OLD_ID = "ts-2026.10.17-draft"
NEW_ID = "ts-2026.10.18-draft"
NEW_IDS = {
    "router.D_px_median": "advisory",
    "router.disocclusion_fraction": "advisory",
    "router.orbit_deg": "advisory",
    "depth_agreement.abs_rel": "advisory",
    "depth_agreement.delta_1": "advisory",
    "depth_agreement.edge_f1": "advisory",
    "set.cross_view_de00_p90": "hard",
    "recolour.de00_median": "advisory",
}


def active() -> thresholds.ThresholdSet:
    return thresholds.load()


# ---------------------------------------------------------------------------- the sets


def test_the_active_set_has_the_new_id_and_the_old_set_still_loads():
    assert active().id == NEW_ID == thresholds.DEFAULT_THRESHOLDS_ID
    assert threshold_sets.available() == [NEW_ID, OLD_ID]
    old = threshold_sets.load_set(OLD_ID)
    assert old.id == OLD_ID and len(old.metrics) == 22
    assert threshold_sets.load_set().id == NEW_ID and threshold_sets.load_set(NEW_ID) is active()


def test_the_old_set_is_exactly_what_it_was():
    old = threshold_sets.load_set(OLD_ID)
    assert not set(NEW_IDS) & set(old.metrics)
    assert old.get("feather_band.de00_p99").status == "hard" and old.get("feather_band.de00_p99").value == 1.0
    assert old.get("texture.lapvar").range == (300, 600) and old.get("video.last_frame").value == 16
    assert old.check("feather_band.de00_p99", 1.5)[0] == "fail"  # hard there, advisory in the active set
    assert active().check("feather_band.de00_p99", 1.5)[0] == "advisory"


def test_the_new_set_is_the_old_set_plus_the_registered_ids_and_the_feather_status():
    old, new = threshold_sets.load_set(OLD_ID), active()
    assert set(new.metrics) == set(old.metrics) | set(NEW_IDS)
    for mid, thr in old.metrics.items():
        a, b = thr.model_dump(), new.metrics[mid].model_dump()
        if mid == "feather_band.de00_p99":
            assert (a.pop("status"), b.pop("status")) == ("hard", "advisory")
            a.pop("provenance"), b.pop("provenance")
        assert a == b, mid


def test_an_unknown_or_malformed_set_id_is_a_keyerror():
    for bad in ("ts-1999", "../thresholds", "..\\thresholds", "ts-2026.10.17-draft/../x", ""):
        with pytest.raises(KeyError):
            threshold_sets.load_set(bad)


def test_the_frozen_file_declares_its_own_id():
    raw = yaml.safe_load((threshold_sets.FROZEN_DIR / f"{OLD_ID}.yaml").read_text(encoding="utf-8"))
    assert raw["set_id"] == OLD_ID and "ensemble" not in raw


@pytest.mark.parametrize("mid,status", sorted(NEW_IDS.items()))
def test_registered_ids_have_provenance_a_bound_and_the_planned_status(mid, status):
    thr = active().get(mid)
    assert thr.status == status and thr.provenance.strip() and thr.unit
    assert thr.value is not None and thr.op in {"le", "ge"}


# ---------------------------------------------------------------------------- equals the module constants


def test_router_numbers_equal_router_math():
    g = active().get
    d = g("router.D_px_median")
    assert d.op == "ge" and d.value == router_math.D_VRAY_ONLY_PX
    assert d.variants == {
        "vray_only_below": router_math.D_VRAY_ONLY_PX,
        "whole_frame_at_or_above": router_math.D_WHOLE_FRAME_PX,
    }
    f = g("router.disocclusion_fraction")
    assert f.op == "le" and f.value == router_math.DISOCC_AI_MAX
    assert f.variants == {
        "ai_interpolate_max": router_math.DISOCC_AI_MAX,
        "rendered_endpoints_plus_clay_max": router_math.DISOCC_CLAY_MAX,
    }
    o = g("router.orbit_deg")
    assert o.op == "le" and o.value == router_math.ORBIT_MAX_DEG


def test_recolour_numbers_equal_the_expected_de00_per_method():
    thr = active().get("recolour.de00_median")
    assert thr.variants == recolour.EXPECTED_DE00
    assert thr.value == recolour.EXPECTED_DE00["recomposition"] and thr.op == "le"
    for method, expected in recolour.EXPECTED_DE00.items():
        assert thresholds.check("recolour.de00_median", expected, variant=method)[0] == "pass"
        assert thresholds.check("recolour.de00_median", expected + 0.1, variant=method)[0] == "advisory"


def test_cross_view_bound_is_what_setgate_reads_and_its_fallback_agrees():
    thr = active().get(setgate.METRIC_ID)
    assert (thr.op, thr.value, thr.status) == ("le", setgate.P90_HARD, "hard")
    read = setgate._threshold()
    assert read == {
        "metric_id": setgate.METRIC_ID,
        "op": "le",
        "value": setgate.P90_HARD,
        "status": "hard",
        "set_id": NEW_ID,
    }


def test_depth_agreement_bounds_are_defined_with_the_modules_own_ratio():
    assert f"{depth_agreement.DELTA_1_RATIO:g}x" in active().get("depth_agreement.delta_1").unit
    assert active().get("depth_agreement.abs_rel").op == "le"
    for mid in ("depth_agreement.delta_1", "depth_agreement.edge_f1"):
        assert active().get(mid).op == "ge"
    # advisory: a miss never fails a report
    assert thresholds.check("depth_agreement.abs_rel", 5.0)[0] == "advisory"
    assert thresholds.check("depth_agreement.delta_1", 0.0)[0] == "advisory"
    assert thresholds.check("depth_agreement.edge_f1", 0.0)[0] == "advisory"


def test_the_ensemble_block_is_the_default_ensemble():
    raw = yaml.safe_load(thresholds._YAML.read_text(encoding="utf-8"))
    assert isinstance(raw["ensemble"], dict) and raw["ensemble"]
    assert set(raw["ensemble"]) <= set(active().metrics)
    loaded = calibration.load_ensemble()
    assert {m: e.weight for m, e in loaded.items()} == calibration.DEFAULT_ENSEMBLE
    assert all(e.scale is None for e in loaded.values())


# ---------------------------------------------------------------------------- every id the code looks up exists


class _Lookups(ast.NodeVisitor):
    """Metric ids a module passes to the threshold API as a literal or as a module-level *METRIC_ID constant."""

    CALLS = {"thresholds.check", "_check_threshold", "ts.check", "self.ts.check", "checks.add"}
    GETS = {"ts", "self.ts", "ts.metrics", "thresholds.load()", "thresholds.load().metrics"}

    def __init__(self, consts: dict[str, dict[str, str]], own: str):
        self.consts, self.own, self.found = consts, own, set()

    def _resolve(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(self.own, {}).get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return self.consts.get(node.value.id, {}).get(node.attr)
        return None

    def visit_Call(self, node: ast.Call) -> None:
        if node.args:
            func = node.func
            name = ast.unparse(func)
            is_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and ast.unparse(func.value) in self.GETS
            )
            if name in self.CALLS or is_get:
                mid = self._resolve(node.args[0])
                if mid is not None:
                    self.found.add(mid)
        self.generic_visit(node)


def _sources() -> list[Path]:
    return sorted((REPO / "gates" / "plate_gates").glob("*.py")) + sorted(
        (REPO / "daemon" / "plate_daemon").rglob("*.py")
    )


def _constants() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("METRIC_ID")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                out.setdefault(path.stem, {})[node.targets[0].id] = node.value.value
    return out


def test_every_metric_id_the_code_looks_up_is_in_the_active_set():
    consts = _constants()
    found: dict[str, set[str]] = {}
    for path in _sources():
        if path.name in {"thresholds.py", "threshold_sets.py"}:
            continue
        visitor = _Lookups(consts, path.stem)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        if visitor.found:
            found[path.relative_to(REPO).as_posix()] = visitor.found
    every = set().union(*found.values())
    assert len(every) >= 8, f"the scan found too little to mean anything: {sorted(every)}"
    assert {"setgate", "invention", "gbuffer", "reprojection"} <= set(consts)
    missing = {
        f: sorted(ids - set(active().metrics)) for f, ids in found.items() if ids - set(active().metrics)
    }
    assert missing == {}, f"looked up but not registered in {NEW_ID}: {missing}"


def test_every_metric_id_constant_is_registered():
    registered = set(active().metrics)
    for stem, names in _constants().items():
        for name, mid in names.items():
            assert mid in registered, f"{stem}.{name} = {mid!r}"
