"""The Wave 9 numeric nodes in the daemon (router plan T93, decision 48): Comp, Colour, Atmosphere and Sky.

The math is held to its vectors on synthetic arrays first (exposure doubles linear light, the neutral grade is the
identity, saturation -100 is grey, the white points sit on their loci, Koschmieder's 2 % at the visibility, an
infinite visibility is no fog, the sky lands on the horizon row). Then each node is driven the way the daemon
drives every node: quote (price 0, local C0), approve (`auto`), queue, the worker's transitions to DONE, a
versioned gate report and a PNG16 in the CAS, on the committed Blender packet `room_a` (a SKY_dome object of
class sky, Z, normals, Cryptomatte). The Comp stacks accepted Colour and Sky takes; the accept note is written as
`POST /runs/{id}/accept` writes it (after a logged scrub), here directly on the run's event log.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import canvas_rules
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.machine import append_note, get_run
from plate_daemon.nodes import (
    KINDS,
    NUMERIC_ENGINE_ID,
    NUMERIC_KINDS,
    NodeRunner,
    atmosphere_node,
    colour_node,
    comp_node,
    install,
    is_node_run,
    sky_node,
)
from plate_daemon.nodes import routes as node_routes
from plate_daemon.nodes import runner as node_runner
from plate_daemon.pipeline import PipelineError, decode_png16, encode_png16

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
SKY = "o:929c9e95"  # SKY_dome (class sky)
PEOPLE = "o:ebb52806"  # PEOPLE_dummy (editable)
PANEL = "o:735a8655"  # ARCH_panel_7016 (locked)
WAVE9 = ("Comp", "Colour", "Atmosphere", "Sky")

needs_packets = pytest.mark.skipif(
    not ((ROOM_A / "manifest.json").is_file() and (ROOM_A / "beauty.exr").is_file()),
    reason="fixtures/packets not built yet (T2)",
)


# ---------------------------------------------------------------------------- reference sRGB (textbook, not the module's)


def _eotf(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _oetf(v: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(v, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def _rand16(seed: int, shape: tuple[int, ...] = (64, 96, 3)) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 65536, size=shape, dtype=np.uint16)


# ---------------------------------------------------------------------------- Colour: the vectors


def test_ev_plus_one_doubles_linear_light():
    src = _rand16(1)
    out = colour_node.grade16(src, {"ev": 1.0})
    lin = _eotf(src / 65535.0)
    room = 2.0 * lin <= 1.0  # values that do not clip at white
    want = np.rint(_oetf(2.0 * lin) * 65535.0)
    assert room.any()
    assert np.abs(out.astype(np.float64) - want)[room].max() <= 2.0
    clipped = ~room
    assert np.all(out[clipped] == 65535)


def test_the_neutral_grade_is_the_identity():
    src = _rand16(2)
    for grade in (
        {},
        {"kelvin": 6500.0, "tint": 0.0},
        {"ev": 0.0, "kelvin": None, "tint": 0.0, "contrast": 0.0, "saturation": 0.0, "hue_deg": 0.0},
    ):
        out = colour_node.grade16(src, grade)
        assert np.abs(out.astype(np.int32) - src.astype(np.int32)).max() <= 1, grade
    assert colour_node.white_balance_matrix(6500.0, 0.0) is None
    assert colour_node.white_balance_matrix(None, 0.0) is None
    # 6501 K is a hair away, not a jump (the neutral is the locus' own 6500 K point)
    m = colour_node.white_balance_matrix(6501.0, 0.0)
    assert np.abs(m - np.eye(3)).max() < 1e-3


def test_saturation_minus_100_is_grey():
    src = _rand16(3)
    out = colour_node.grade16(src, {"saturation": -100.0}).astype(np.int32)
    assert np.abs(out[..., 0] - out[..., 1]).max() <= 1 and np.abs(out[..., 1] - out[..., 2]).max() <= 1
    # the grey is the Rec.709 luma of the linear input
    lin = _eotf(src / 65535.0)
    luma = lin @ np.array([0.2126, 0.7152, 0.0722])
    assert np.abs(out[..., 1] - np.rint(_oetf(luma) * 65535.0)).max() <= 1


def test_white_points_sit_on_their_loci():
    d65 = (0.31271, 0.32902)
    x, y = colour_node.locus_xy(6500.0)
    assert (
        abs(x - d65[0]) < 5e-4 and abs(y - d65[1]) < 5e-4
    )  # the daylight formula's 6500 K is D65's neighbour
    x, y = colour_node.locus_xy(5003.0)  # D50 on the daylight locus
    assert abs(x - 0.3457) < 2e-3 and abs(y - 0.3585) < 2e-3
    x, y = colour_node.locus_xy(2856.0)  # Illuminant A on the Planckian locus (Kang 2002)
    assert abs(x - 0.4476) < 2e-3 and abs(y - 0.4074) < 2e-3
    # a tint moves along the isotherm: |delta uv| = tint/100 x 0.05, perpendicular to the locus, magenta = lower v
    u0, v0 = colour_node.xy_to_uv(*colour_node.locus_xy(5000.0))
    u1, v1 = colour_node.xy_to_uv(*colour_node.white_xy(5000.0, 100.0))
    assert math.hypot(u1 - u0, v1 - v0) == pytest.approx(0.05, abs=1e-9) and v1 < v0
    ua, va = colour_node.xy_to_uv(*colour_node.locus_xy(4999.0))
    ub, vb = colour_node.xy_to_uv(*colour_node.locus_xy(5001.0))
    tangent = np.array([ub - ua, vb - va])
    assert abs(float(np.dot(tangent / np.linalg.norm(tangent), [u1 - u0, v1 - v0]))) < 1e-6


def test_bradford_takes_the_source_white_to_the_target_white():
    src, dst = colour_node.locus_xy(6500.0), colour_node.white_xy(3000.0, 20.0)

    def xyz(xy):
        return np.array([xy[0] / xy[1], 1.0, (1 - xy[0] - xy[1]) / xy[1]])

    m = colour_node.bradford(src, dst)
    assert np.allclose(m @ xyz(src), xyz(dst), atol=1e-12)
    grey = np.full((1, 1, 3), 0.5)
    warm = colour_node.grade_linear(grey, {"kelvin": 3000.0})[0, 0]
    cool = colour_node.grade_linear(grey, {"kelvin": 10000.0})[0, 0]
    assert warm[0] > warm[1] > warm[2] and cool[2] > cool[0]  # 3000 K lights the frame warm, 10000 K cool
    magenta = colour_node.grade_linear(grey, {"tint": 50.0})[0, 0]
    assert magenta[1] < magenta[0] and magenta[1] < magenta[2]


def test_hue_rotation_keeps_luma_and_contrast_pivots_on_mid_grey():
    lin = _eotf(_rand16(4, (32, 32, 3)) / 65535.0)
    luma = np.array([0.2126, 0.7152, 0.0722])
    turned = colour_node.grade_linear(lin, {"hue_deg": 120.0})
    assert np.allclose(turned @ luma, lin @ luma, atol=1e-12)
    assert not np.allclose(turned, lin)
    full = colour_node.grade_linear(lin, {"hue_deg": 180.0})
    back = colour_node.grade_linear(full, {"hue_deg": 180.0})
    assert np.allclose(back, lin, atol=1e-9)  # two half turns are a whole one
    mid = np.full((1, 1, 3), 0.18)
    assert np.allclose(colour_node.grade_linear(mid, {"contrast": 40.0}), 0.18)
    bright = colour_node.grade_linear(np.full((1, 1, 3), 0.5), {"contrast": 40.0})
    assert np.all(bright > 0.5)


# ---------------------------------------------------------------------------- Atmosphere: the vectors


def test_koschmieder_at_the_visibility_is_two_percent_contrast():
    vis = 800.0
    z = np.full((4, 4), vis)
    f = atmosphere_node.fog_fraction(z, atmosphere_node.KOSCHMIEDER / vis, 0.6)
    assert np.allclose(f, 0.980, atol=1e-3)
    # sky, no depth, the background: the sky fog
    z = np.array([[0.0, -1.0, np.inf, np.nan, 10.0]])
    sky = np.array([[0.0, 0.0, 0.0, 0.0, 1.0]])
    f = atmosphere_node.fog_fraction(z, 0.01, 0.6, sky)
    assert np.allclose(f, 0.6)
    valid = np.array([[True, True, True, True, False]])
    f = atmosphere_node.fog_fraction(np.full((1, 5), 5.0), 0.01, 0.25, None, valid)
    assert f[0, -1] == 0.25 and np.allclose(f[0, :4], 1 - math.exp(-0.05))


def test_an_infinite_visibility_is_no_fog():
    src = _rand16(5)
    z = np.random.default_rng(5).uniform(1.0, 200.0, src.shape[:2])
    f = atmosphere_node.fog_fraction(z, atmosphere_node.KOSCHMIEDER / 1e9, 0.6)
    out = atmosphere_node.fog16(src, f, [0.75, 0.78, 0.82])
    assert np.abs(out.astype(np.int32) - src.astype(np.int32)).max() <= 1
    full = atmosphere_node.fog16(src, np.ones(src.shape[:2]), [0.5, 0.5, 0.5])
    assert np.all(full == np.rint(_oetf(0.5) * 65535))


# ---------------------------------------------------------------------------- Sky: the vectors


def _camera(pitch_deg: float, w: int = 640, h: int = 360, fx: float = 622.2) -> dict[str, Any]:
    """A level camera at (0, 0, 1.5) looking along +Y, tilted down by `pitch_deg` (plate-cam-v0)."""
    a = math.radians(pitch_deg)
    forward = np.array([0.0, math.cos(a), -math.sin(a)])
    right = np.array([1.0, 0.0, 0.0])
    up = np.cross(right, forward)
    back = -forward
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = right, up, back, [0.0, 0.0, 1.5]
    return {
        "world_from_camera": m.tolist(),
        "intrinsics_px": {"fx": fx, "fy": fx, "principal_point": [w / 2, h / 2]},
        "convention": {"world_up": "+Z"},
    }


def test_the_horizon_row_of_a_tilted_camera():
    assert sky_node.horizon_y(_camera(0.0)) == pytest.approx(180.0, abs=1e-6)
    for pitch in (-8.0, 3.5, 12.0):
        want = 180.0 - 622.2 * math.tan(math.radians(pitch))  # looking down puts the horizon above the centre
        assert sky_node.horizon_y(_camera(pitch)) == pytest.approx(want, abs=1e-6)
    with pytest.raises(PipelineError) as e:
        sky_node.horizon_y(_camera(90.0))
    assert e.value.code == "no_horizon"


def test_the_photo_row_lands_on_the_horizon():
    h, w = 40, 64
    photo = np.repeat(np.arange(100, dtype=np.float32)[:, None, None] * 300.0, w, axis=1).repeat(3, axis=2)
    where = sky_node.placement(w, (w, 100), y_h=20.25, horizon_frac=0.5, dx_px=0.0)
    got = sky_node.place(photo, (h, w), where, (0, h, 0, w))
    rows = np.arange(h) - where["y0"]  # photo row under each plate row
    assert np.allclose(got[:, 7, 0], np.clip(rows, 0, 99) * 300.0, atol=1e-2)
    assert got[20, 0, 0] == pytest.approx(49.75 * 300.0, abs=1e-2)  # plate row 20 (centre 20.5) = photo 49.75
    small = sky_node.resized(np.zeros((100, 128, 3), np.uint16), 64, 1.0)
    assert small.shape == (50, 64, 3)


# ---------------------------------------------------------------------------- the daemon around the nodes


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    pipeline: Any
    runner: NodeRunner

    def project(self, project_id: str = PROJECT) -> None:
        r = self.client.post(
            "/api/v0/projects", json={"project_id": project_id, "custody": {"nda": False}}, headers=self.auth
        )
        assert r.status_code == 201, r.text

    def packet(self, path: Path) -> str:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**self.auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(path), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        assert (
            self.client.get(f"/api/v0/packets/{packet_id}", headers=self.auth).json()["status"] == "ingested"
        )
        return packet_id

    def graph(self, nodes: list[dict[str, Any]], edges: list[Any], project_id: str = PROJECT) -> None:
        wire = []
        for e in edges:
            a, b, *handle = e
            edge = {"id": f"{a}->{b}", "source": a, "target": b}
            if handle:
                edge["targetHandle"] = handle[0]
            wire.append(edge)
        r = self.client.put(
            f"/api/v0/projects/{project_id}/graph", json={"nodes": nodes, "edges": wire}, headers=self.auth
        )
        assert r.status_code == 200, r.text

    @property
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def make(self, node_id: str, body: dict[str, Any] | None = None, project_id: str = PROJECT):
        with self.pipeline.index.project(project_id) as conn:
            return self.runner.make(conn, project_id, node_id, body)

    def accept(self, run_id: str) -> None:
        with self.pipeline.index.project(PROJECT) as conn:
            note = {"scrub_log_id": "scrub_test", "decision": "accepted"}
            append_note(conn, run_id, kind="accept", actor="human", reason=json.dumps(note, sort_keys=True))

    def run(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return get_run(conn, run_id)

    def report(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/v0/runs/{run_id}/gate", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def png(self, key: str) -> np.ndarray:
        return decode_png16(self.cas.read_bytes(key))

    def put_png(self, rgb16: np.ndarray) -> str:
        return self.cas.put_bytes(encode_png16(rgb16), "png")

    def preview(self, node_id: str, what: str, params: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/preview",
            json={"what": what, "params": params or {}},
            headers=self.auth,
        )


@pytest.fixture
def env(plate_env, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        runner = install(pipeline)
        if not any(getattr(r, "path", "").endswith("/nodes/{node_id}/preview") for r in client.app.routes):
            client.app.include_router(node_routes.router)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, settings, pipeline, runner)


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


def _alpha() -> np.ndarray:
    from plate_gates.material_de import id_to_u32
    from plate_pixels.exr import read_cryptomatte

    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    ranks, cov = read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    return np.clip(np.where(ranks == np.uint32(id_to_u32(SKY)), cov, 0.0).sum(axis=2), 0.0, 1.0)


def _check(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return next(c for c in report["checks"] if c["metric_id"] == metric_id)


def _gradient_sky(h: int = 360, w: int = 640) -> np.ndarray:
    """A synthetic sky: blue rising with height, 16 bit, a linear ramp down the rows."""
    rows = np.linspace(52000.0, 20000.0, h)[::-1][:, None]  # brighter towards the bottom (the horizon)
    sky = np.empty((h, w, 3), dtype=np.float64)
    sky[..., 0] = rows * 0.55
    sky[..., 1] = rows * 0.75
    sky[..., 2] = rows
    return np.rint(sky).astype(np.uint16)


def _one(
    env: Env,
    kind: str,
    params: dict[str, Any],
    extra: list[dict[str, Any]] | None = None,
    edges: list[Any] | None = None,
) -> str:
    env.project()
    packet_id = env.packet(ROOM_A)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "n", "kind": kind, "label": kind, "params": params},
            *(extra or []),
        ],
        [("p", "n"), *(edges or [])],
    )
    return packet_id


# ---------------------------------------------------------------------------- the registry


def test_the_registry_holds_the_four():
    assert set(WAVE9) <= NUMERIC_KINDS <= KINDS
    assert {"set", "lightmix", "recolour", "comp", "colour", "atmosphere", "sky"} <= set(node_runner.PREVIEWS)
    for kind, module in zip(WAVE9, (comp_node, colour_node, atmosphere_node, sky_node), strict=True):
        assert node_runner.MODULES[kind] is module and module.KIND == kind
        for name in ("prepare", "normalize", "preview", "gate", "score"):
            assert callable(getattr(module, name)), (kind, name)
    assert set(WAVE9) <= set(canvas_rules.NODE_KINDS)
    assert not (set(WAVE9) & canvas_rules.GENERATING_KINDS)


# ---------------------------------------------------------------------------- Colour on room_a


@needs_packets
def test_colour_ev_on_ids_doubles_inside_and_leaves_the_rest(env):
    _one(env, "Colour", {"scope": "ids", "ids": [PEOPLE], "ev": 1.0})
    run = env.make("n")
    assert run.state.value == "DONE", run.error
    assert is_node_run(run) and run.route.engine_id == NUMERIC_ENGINE_ID
    assert run.quote.price_usd == 0.0 and run.quote.custody_grade == "C0" and run.approved_by == "auto"
    plate, out = _plate16(), env.png(run.outputs[0])
    with env.pipeline.index.project(PROJECT) as conn:
        pv = env.pipeline.packet(PROJECT, get_run(conn, run.id).packet_id)
    scope = pv.id_mask(PEOPLE)
    assert scope.any() and np.array_equal(
        out[~scope], plate[~scope]
    )  # outside the scope: the plate, bit for bit
    lin = _eotf(plate / 65535.0)
    room = scope[..., None] & (2.0 * lin <= 1.0)
    want = np.rint(_oetf(2.0 * lin) * 65535.0)
    assert np.abs(out.astype(np.float64) - want)[room].max() <= 2.0
    assert run.normalized["graded"]["ev"] == 1.0 and run.normalized["scope"]["px"] == int(scope.sum())
    report = env.report(run.id)
    assert report["verdict"] in ("pass", "advisory"), report["checks"]
    assert _check(report, "outside_mask.changed_px")["value"] == 0.0
    assert _check(report, "outside_mask.changed_px")["status"] == "pass"
    for c in report["checks"]:
        if c["metric_id"].startswith(("material.", "texture.", "tone.")):
            assert c["status"] != "fail", c
    assert _check(report, "gbuffer_edge_recall_r2")["status"] == "pass"
    assert report["metrics"]["registration"]["source"] == "numeric"
    assert any("advisory" in n and "material" in n for n in report["backend_notes"])


@needs_packets
def test_colour_neutral_is_the_plate_and_a_frame_desaturation_is_grey(env):
    _one(env, "Colour", {"kelvin": 6500, "tint": 0})
    run = env.make("n")
    assert run.state.value == "DONE", run.error
    plate = _plate16()
    assert np.abs(env.png(run.outputs[0]).astype(np.int32) - plate.astype(np.int32)).max() <= 1
    assert run.normalized["scope"]["kind"] == "frame"  # no Mask wired: the whole frame

    grey = env.make("n", {"params": {"saturation": -100}})
    assert grey.state.value in ("DONE", "GATE_FAIL"), grey.error
    out = env.png(grey.outputs[0]).astype(np.int32)
    assert np.abs(out[..., 0] - out[..., 1]).max() <= 1 and np.abs(out[..., 1] - out[..., 2]).max() <= 1
    report = env.report(grey.id)
    assert _check(report, "material.de00_p90")["status"] == "advisory"  # a grade moves materials by design


@needs_packets
def test_colour_scope_follows_the_wired_mask(env):
    mask = {"id": "m", "kind": "Mask", "label": "Mask", "params": {"ids": [PEOPLE]}}
    _one(env, "Colour", {"saturation": -100}, [mask], [("p", "m"), ("m", "n", "mask")])
    run = env.make("n")
    assert run.state.value == "DONE", run.error
    assert run.normalized["scope"]["kind"] == "mask" and run.normalized["scope"]["node"] == "m"
    scope = env.png(run.normalized["mask_key"])[..., 0] > 0
    assert np.array_equal(env.png(run.outputs[0])[~scope], _plate16()[~scope])
    # a scope the artist names a locked id in is graded, and the report says so
    told = env.make("n", {"params": {"scope": "ids", "ids": [PANEL], "ev": 0.5}})
    assert told.state.value == "DONE", told.error
    assert PANEL in env.report(told.id)["metrics"]["colour"]["locked_in_scope"]


@needs_packets
def test_colour_refuses_bad_params(env):
    _one(env, "Colour", {})
    for params, code in (
        ({"ev": 5}, "bad_params"),
        ({"kelvin": 1500}, "bad_params"),
        ({"scope": "mask"}, "no_mask"),
        ({"scope": "ids"}, "bad_params"),
        ({"scope": "ids", "ids": ["o:00000000"]}, "unknown_id"),
        ({"scope": "nowhere"}, "bad_params"),
    ):
        with pytest.raises(PipelineError) as e:
            env.make("n", {"params": params})
        assert e.value.code == code, params


# ---------------------------------------------------------------------------- Atmosphere on room_a


@needs_packets
def test_atmosphere_fog_follows_z(env):
    _one(env, "Atmosphere", {"visibility_m": 50, "colour": [0.8, 0.8, 0.8], "sky_fog": 0.5})
    run = env.make("n")
    assert run.state.value in ("DONE", "GATE_FAIL"), run.error
    info = run.normalized["atmosphere"]
    assert info["beta_per_m"] == pytest.approx(3.912 / 50) and info["colour_source"] == "params"
    plate, out = _plate16(), env.png(run.outputs[0])
    lin, fog = _eotf(plate / 65535.0), _eotf(out / 65535.0)
    from plate_gates import crossview

    view = crossview.PacketView.load(ROOM_A)
    sky = _alpha()
    gap = 0.8 - lin
    ch = np.argmax(np.abs(gap), axis=2)
    pick = np.take_along_axis
    g = pick(gap, ch[..., None], 2)[..., 0]
    d = pick(fog - lin, ch[..., None], 2)[..., 0]
    usable = view.valid() & (sky == 0) & (np.abs(g) > 0.1)
    f_seen = d[usable] / g[usable]
    r = np.corrcoef(f_seen, view.z[usable])[0, 1]
    assert usable.sum() > 1000 and r > 0.9, r
    expected = 1.0 - np.exp(-3.912 / 50 * view.z[usable])
    assert np.median(np.abs(f_seen - expected)) < 0.01
    # sky pixels take the sky fog
    full = sky >= 1.0
    want = np.rint(_oetf(lin + (0.8 - lin) * 0.5) * 65535.0)
    assert np.abs(out.astype(np.float64) - want)[full].max() <= 1.0
    report = env.report(run.id)
    assert report["metrics"]["atmosphere"]["visibility_m"] == 50
    assert _check(report, "gbuffer_edge_recall_r2")["status"] in ("pass", "fail")  # structure is scored, hard
    for c in report["checks"]:
        if c["metric_id"].startswith(("material.", "texture.", "tone.")):
            assert c["status"] != "fail", c


@needs_packets
def test_atmosphere_default_is_gentle_and_takes_the_horizon_colour(env):
    _one(env, "Atmosphere", {})
    run = env.make("n")
    assert run.state.value == "DONE", [c for c in env.report(run.id)["checks"] if c["status"] == "fail"]
    info = run.normalized["atmosphere"]
    assert info["visibility_m"] == 800 and info["sky_fog"] == 0.6 and info["colour_source"] == "sky_band"
    report = env.report(run.id)
    assert report["verdict"] in ("pass", "advisory")
    assert _check(report, "gbuffer_edge_recall_r2")["status"] == "pass"
    r = env.preview("n", "atmosphere", {"visibility_m": 200})
    assert r.status_code == 200, r.text
    assert r.json()["visibility_m"] == 200 and 0.0 <= r.json()["f"]["min"] <= r.json()["f"]["max"] <= 1.0


@needs_packets
def test_atmosphere_refuses_out_of_range_and_tier_c(env):
    _one(env, "Atmosphere", {})
    for params in ({"visibility_m": 5}, {"visibility_m": 1e9}, {"sky_fog": 2}, {"colour": [1, 2]}):
        with pytest.raises(PipelineError) as e:
            env.make("n", {"params": params})
        assert e.value.code == "bad_params", params
    assert _tier_c_code(env, "Atmosphere") == "no_z"


def _tier_c_code(env: Env, kind: str) -> str:
    import plate_spec

    packet = plate_spec.materialize_example("tierC_client_plate", env.settings.packets_dirs[0] / f"tc_{kind}")
    project = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))["project_id"]
    env.project(project)
    packet_id = env.packet(packet)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "C", "label": "Plate"},
            {"id": "n", "kind": kind, "label": kind, "params": {"skyHash": "0" * 64}},
        ],
        [("p", "n")],
        project_id=project,
    )
    with pytest.raises(PipelineError) as e:
        env.make("n", project_id=project)
    return e.value.code


# ---------------------------------------------------------------------------- Sky on room_a


def _sky_graph(env: Env, params: dict[str, Any] | None = None) -> str:
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.put_png(_gradient_sky())
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "r", "kind": "Reference", "label": "Sky", "params": {"referenceHash": key, "role": "sky"}},
            {"id": "n", "kind": "Sky", "label": "Sky", "params": params or {}},
        ],
        [("p", "n"), ("r", "n", "sky")],
    )
    return key


@needs_packets
def test_sky_on_room_a_lands_inside_the_sky_coverage(env):
    _sky_graph(env, {"tone_match": False})
    run = env.make("n")
    assert run.state.value == "DONE", run.error
    plate, out, alpha = _plate16(), env.png(run.outputs[0]), _alpha()
    assert np.array_equal(out[alpha == 0], plate[alpha == 0])  # bit-identical where there is no sky
    info = run.normalized["sky"]
    # where the sky is whole, the output is the photo resampled onto the horizon: the ramp, row by row
    y0 = info["placement"]["y0"]
    ramp = _gradient_sky().astype(np.float64)
    rows = np.clip(np.arange(360) - y0, 0, 359)
    lo = np.floor(rows).astype(int)
    hi = np.minimum(lo + 1, 359)
    t = (rows - lo)[:, None, None]
    want = ramp[lo] * (1 - t) + ramp[hi] * t
    full = alpha == 1.0
    assert full.sum() > 1000
    assert np.abs(out.astype(np.float64) - want)[full].max() <= 1.0
    # the horizon is the camera's: cy - fy tan(tilt down)
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    cam = manifest["camera"]
    m = np.asarray(cam["world_from_camera"])
    forward = -m[:3, 2] / np.linalg.norm(m[:3, 2])
    tilt_down = math.asin(-forward[2])
    k = cam["intrinsics_px"]
    want_y = k["principal_point"][1] - k["fy"] * math.tan(tilt_down)
    assert abs(info["horizon_y"] - want_y) <= 0.5
    assert y0 == pytest.approx(info["horizon_y"] - 0.5 * 360)
    report = env.report(run.id)
    assert report["verdict"] in ("pass", "advisory"), [c for c in report["checks"] if c["status"] == "fail"]
    assert _check(report, "outside_mask.changed_px")["value"] == 0.0
    assert run.normalized["mask_px"] == int((alpha > 0).sum())


@needs_packets
def test_sky_tone_match_moves_the_photo_to_the_plate_mean(env):
    from plate_gates.color import srgb_to_lab

    _sky_graph(env)
    run = env.make("n")
    assert run.state.value == "DONE", run.error
    alpha = _alpha()
    core = alpha >= 1.0
    out, plate = env.png(run.outputs[0]), _plate16()
    got = srgb_to_lab(out[core] / 257.0).mean(axis=0)
    want = srgb_to_lab(plate[core] / 257.0).mean(axis=0)
    assert np.abs(got - want).max() < 1.0
    assert run.normalized["sky"]["tone_shift_lab"] is not None
    r = env.preview("n", "sky", {"dx_px": 12})
    assert r.status_code == 200, r.text
    assert r.json()["placement"]["x0"] == pytest.approx(12.0) and r.json()["alpha_px"] == int(
        (alpha > 0).sum()
    )


@needs_packets
def test_sky_refusals(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "n", "kind": "Sky", "label": "Sky", "params": {}},
        ],
        [("p", "n")],
    )
    with pytest.raises(PipelineError) as e:
        env.make("n")
    assert e.value.code == "no_sky_reference"
    with pytest.raises(PipelineError) as e:
        env.make("n", {"params": {"skyHash": "0" * 64}})
    assert e.value.code == "reference_missing"
    key = env.put_png(_gradient_sky())
    with pytest.raises(PipelineError) as e:
        env.make("n", {"params": {"skyHash": key, "scale": 0}})
    assert e.value.code == "bad_params"
    ok = env.make(
        "n", {"params": {"skyHash": key, "scale": 0.5, "tone_match": False}}
    )  # params.skyHash works too
    assert ok.state.value == "DONE", ok.error
    assert ok.normalized["sky"]["placement"]["width"] == 320
    assert _tier_c_code(env, "Sky") == "no_cryptomatte"


# ---------------------------------------------------------------------------- Comp on room_a


def _comp_graph(env: Env, comp_params: dict[str, Any] | None = None) -> dict[str, str]:
    """Plate -> Colour (people +1 EV), Plate -> Colour frame (saturation -100), Plate + Reference -> Sky, and a
    Comp over the Plate with the three as layers. Makes the three layer runs; returns their run ids."""
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.put_png(_gradient_sky())
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {
                "id": "people",
                "kind": "Colour",
                "label": "People",
                "params": {"scope": "ids", "ids": [PEOPLE], "ev": 1},
            },
            {
                "id": "grey",
                "kind": "Colour",
                "label": "Grey",
                "params": {"scope": "ids", "ids": [SKY], "saturation": -100},
            },
            {
                "id": "r",
                "kind": "Reference",
                "label": "Sky ref",
                "params": {"referenceHash": key, "role": "sky"},
            },
            {"id": "sky", "kind": "Sky", "label": "Sky", "params": {"tone_match": False}},
            {"id": "comp", "kind": "Comp", "label": "Comp", "params": comp_params or {}},
        ],
        [
            ("p", "people"),
            ("p", "grey"),
            ("p", "sky"),
            ("r", "sky", "sky"),
            ("p", "comp", "base"),
            ("people", "comp", "layers"),
            ("grey", "comp", "layers"),
            ("sky", "comp", "layers"),
        ],
    )
    runs = {}
    for nid in ("people", "grey", "sky"):
        run = env.make(nid)
        assert run.state.value == "DONE", (nid, run.error)
        runs[nid] = run.id
    return runs


@needs_packets
def test_comp_refuses_unaccepted_layers_through_the_quote_route(env):
    runs = _comp_graph(env)
    r = env.client.post(f"/api/v0/projects/{PROJECT}/nodes/comp/quote", json={}, headers=env.auth)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "LAYER_NOT_ACCEPTED" and r.json()["node"] == "people"
    for run_id in runs.values():
        env.accept(run_id)
    r = env.client.post(f"/api/v0/projects/{PROJECT}/nodes/comp/quote", json={}, headers=env.auth)
    assert r.status_code == 200, r.text
    assert r.json()["run"]["state"] == "QUOTED" and r.json()["quote"]["price_usd"] == 0.0
    assert r.json()["quote"]["custody_grade"] == "C0"
    run_id = r.json()["run"]["id"]
    r = env.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=env.auth)
    assert r.status_code == 200, r.text
    assert env.client.get(f"/api/v0/runs/{run_id}", headers=env.auth).json()["state"] == "DONE"


@needs_packets
def test_comp_stacks_layers_in_order_inside_their_masks(env):
    runs = _comp_graph(env, {"require_accept": False})
    comp = env.make("comp")
    assert comp.state.value == "DONE", comp.error
    plate, out = _plate16(), env.png(comp.outputs[0])
    norm = comp.normalized
    assert [row["node"] for row in norm["layers"]] == ["people", "grey", "sky"]
    assert [row["run"] for row in norm["layers"]] == [runs["people"], runs["grey"], runs["sky"]]
    union = env.png(norm["mask_union_key"])[..., 0] > 0
    assert np.array_equal(out[~union], plate[~union])  # outside the union: the plate, bit for bit
    sky_take = env.png(env.run(runs["sky"]).outputs[0])
    people_take = env.png(env.run(runs["people"]).outputs[0])
    sky_mask = env.png(norm["layers"][2]["mask_key"])[..., 0] > 0
    people_mask = env.png(norm["layers"][0]["mask_key"])[..., 0] > 0
    assert np.array_equal(out[sky_mask], sky_take[sky_mask])  # the last layer wins where it is
    only_people = people_mask & ~sky_mask & ~(env.png(norm["layers"][1]["mask_key"])[..., 0] > 0)
    assert only_people.any() and np.array_equal(out[only_people], people_take[only_people])
    assert all(row["mask_source"] == "stored" for row in norm["layers"])  # the Wave 9 nodes store their masks
    report = env.report(comp.id)
    assert report["verdict"] in ("pass", "advisory"), [c for c in report["checks"] if c["status"] == "fail"]
    assert report["metrics"]["outside_mask"]["identical_outside"] is True

    # params.order: grey after sky, so the grey layer wins on the sky
    again = env.make("comp", {"params": {"require_accept": False, "order": ["sky", "grey"]}})
    assert again.state.value == "DONE", again.error
    assert [row["node"] for row in again.normalized["layers"]] == ["sky", "grey", "people"]
    grey_take = env.png(env.run(runs["grey"]).outputs[0])
    grey_mask = env.png(again.normalized["layers"][1]["mask_key"])[..., 0] > 0
    assert np.array_equal(env.png(again.outputs[0])[grey_mask], grey_take[grey_mask])
    r = env.preview("comp", "comp", {"require_accept": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["node"] for row in body["layers"]] == ["people", "grey", "sky"]
    assert body["overlap_px"] > 0  # grey and sky both claim the sky
    assert 0 < body["layers"][2]["coverage"] < 1


@needs_packets
def test_comp_uses_the_changed_pixels_of_a_layer_without_a_mask(env):
    """A numeric take that stored no mask (Recolour, LightMix, Set) composites by its exact changed pixels."""
    runs = _comp_graph(env, {"require_accept": False})
    with env.pipeline.index.project(PROJECT) as conn:
        sky_run = get_run(conn, runs["sky"])
    norm = dict(sky_run.normalized)
    norm.pop("mask_key")
    with env.pipeline.index.project(PROJECT) as conn:
        conn.execute("UPDATE runs SET normalized = ? WHERE id = ?", (json.dumps(norm), sky_run.id))
        conn.commit()
    comp = env.make("comp")
    assert comp.state.value == "DONE", comp.error
    row = comp.normalized["layers"][2]
    assert row["mask_source"] == "changed"
    changed = np.any(env.png(sky_run.outputs[0]) != _plate16(), axis=2)
    assert np.array_equal(env.png(row["mask_key"])[..., 0] > 0, changed)


@needs_packets
def test_comp_needs_layers_with_takes(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "c", "kind": "Colour", "label": "Colour", "params": {"ev": 1}},
            {"id": "comp", "kind": "Comp", "label": "Comp", "params": {}},
        ],
        [("p", "c"), ("p", "comp", "base")],
    )
    with pytest.raises(PipelineError) as e:
        env.make("comp")
    assert e.value.code == "comp_needs_layers"
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "c", "kind": "Colour", "label": "Colour", "params": {"ev": 1}},
            {"id": "comp", "kind": "Comp", "label": "Comp", "params": {}},
        ],
        [("p", "c"), ("p", "comp", "base"), ("c", "comp", "layers")],
    )
    with pytest.raises(PipelineError) as e:
        env.make("comp")
    assert e.value.code == "LAYER_NOT_READY" and e.value.status == 409
    with pytest.raises(PipelineError) as e:
        env.make("comp", {"params": {"order": ["nope"]}})
    assert e.value.code == "bad_params"


@needs_packets
def test_a_colour_on_a_comp_grades_the_stack(env):
    """A Colour whose image is the Comp's take: graded inside its scope, the Comp's pixels everywhere else."""
    runs = _comp_graph(env, {"require_accept": False})
    comp = env.make("comp")
    assert comp.state.value == "DONE", comp.error
    graph = env.client.get(f"/api/v0/projects/{PROJECT}/graph", headers=env.auth).json()
    graph["nodes"].append(
        {
            "id": "post",
            "kind": "Colour",
            "label": "Post",
            "params": {"scope": "ids", "ids": [PEOPLE], "ev": -1},
        }
    )
    graph["edges"].append({"id": "comp->post", "source": "comp", "target": "post"})
    r = env.client.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=env.auth)
    assert r.status_code == 200, r.text
    post = env.make("post")
    assert post.state.value == "DONE", post.error
    assert post.normalized["source"]["run"] == comp.id
    scope = env.png(post.normalized["mask_key"])[..., 0] > 0
    assert np.array_equal(env.png(post.outputs[0])[~scope], env.png(comp.outputs[0])[~scope])
    union = env.png(post.normalized["mask_union_key"])[..., 0] > 0
    assert np.array_equal(env.png(post.outputs[0])[~union], _plate16()[~union])
    assert runs  # the layers stay as they were


# ---------------------------------------------------------------------------- invention flags: tone-invariant, still hard

WALL = "o:57b3d007"  # ARCH_wall_N: planar architecture, scanned by the invention detector


def _wall_box() -> np.ndarray:
    """A painted mask: a 60 x 44 box well inside ARCH_wall_N (asserted, so a rebuilt fixture cannot move it)."""
    from plate_gates.material_de import id_to_u32
    from plate_pixels.exr import read_cryptomatte

    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    ranks, _cov = read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    wall = ranks[..., 0] == np.uint32(id_to_u32(WALL))
    box = np.zeros(wall.shape, dtype=bool)
    box[36:80, 412:472] = True
    assert wall[28:88, 404:480].all(), "the box (and its border) must sit inside the wall"
    return box


def _put_mask(env: Env, mask: np.ndarray) -> str:
    import cv2

    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    assert ok
    return env.cas.put_bytes(bytes(buf), "png")


@needs_packets
def test_a_frame_grade_lifts_plate_edges_but_invents_nothing(env):
    """+1 EV over the whole frame: the plate-subtracted detector alone flags the plate's own weak edges (lifted
    over its threshold); scored with the plate under the same grade as known, the grade invents nothing."""
    from plate_gates import gbuffer, invention
    from plate_gates.io import to_uint8

    _one(env, "Colour", {"ev": 1.0, "contrast": 20})
    run = env.make("n")
    assert run.state.value == "DONE", [c for c in env.report(run.id)["checks"] if c["status"] == "fail"]
    report = env.report(run.id)
    assert (
        _check(report, "invention_flags")["value"] == 0.0
        and _check(report, "invention_flags")["status"] == "pass"
    )
    assert "the plate under this grade" in report["metrics"]["invention"]["known"]
    emap, ctx = gbuffer.from_packet(ROOM_A)
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    raw = invention.report(
        to_uint8(env.png(run.outputs[0])), emap, ctx["ids"], legend, plate_rgb=to_uint8(_plate16()), tier="A"
    )
    assert raw["count"] > 0  # what the check would have said against the ungraded plate


@needs_packets
def test_a_grade_step_across_a_wall_is_an_invention(env):
    """A painted box on a planar wall graded +1 EV draws a line the render does not have: hard fail."""
    box = _wall_box()
    key = _put_mask(env, box)
    mask = {"id": "m", "kind": "Mask", "label": "Mask", "params": {"maskHash": key}}
    _one(env, "Colour", {"ev": 1.0}, [mask], [("p", "m"), ("m", "n", "mask")])
    run = env.make("n")
    assert run.state.value == "GATE_FAIL", run.error
    report = env.report(run.id)
    inv = _check(report, "invention_flags")
    assert inv["status"] == "fail" and inv["value"] >= 1
    assert {f["id"] for f in report["metrics"]["invention"]["flags"]} == {WALL}
    assert any(p["metric_id"] == "invention_flags" for p in report["flagged_points"])
    assert _check(report, "outside_mask.changed_px")["value"] == 0.0  # still nothing outside the box

    # A GATE_FAIL take is a Comp layer only with a ledgered override.
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": run.packet_id, "tier": "A", "label": "Plate"},
            mask,
            {"id": "n", "kind": "Colour", "label": "Colour", "params": {"ev": 1.0}},
            {"id": "comp", "kind": "Comp", "label": "Comp", "params": {"require_accept": False}},
        ],
        [("p", "m"), ("m", "n", "mask"), ("p", "n"), ("p", "comp", "base"), ("n", "comp", "layers")],
    )
    with pytest.raises(PipelineError) as e:
        env.make("comp")
    assert e.value.code == "LAYER_NOT_READY"
    r = env.client.post(
        f"/api/v0/runs/{run.id}/override",
        json={"reason": "the box is the brief: a lit niche", "by": "human"},
        headers=env.auth,
    )
    assert r.status_code == 200, r.text
    comp = env.make("comp")
    assert comp.normalized["layers"][0]["run"] == run.id
    # the take was judged (and overridden) on its own: stacked as it is, the Comp draws nothing new
    assert comp.state.value == "DONE", [c for c in env.report(comp.id)["checks"] if c["status"] == "fail"]
    assert np.array_equal(env.png(comp.outputs[0]), env.png(run.outputs[0]))


@needs_packets
def test_a_comp_seam_across_a_wall_is_an_invention(env):
    """A frame-graded take stacked through a box mask: the step at the box's border is in neither the take nor the
    plate, so the Comp drew it, and it fails its own gate."""
    _one(env, "Colour", {"ev": 1.0})
    layer = env.make("n")
    assert layer.state.value == "DONE", layer.error
    key = _put_mask(env, _wall_box())
    norm = {**layer.normalized, "mask_key": key, "mask_union_key": key}
    with env.pipeline.index.project(PROJECT) as conn:
        conn.execute("UPDATE runs SET normalized = ? WHERE id = ?", (json.dumps(norm), layer.id))
        conn.commit()
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": layer.packet_id, "tier": "A", "label": "Plate"},
            {"id": "n", "kind": "Colour", "label": "Colour", "params": {"ev": 1.0}},
            {"id": "comp", "kind": "Comp", "label": "Comp", "params": {"require_accept": False}},
        ],
        [("p", "n"), ("p", "comp", "base"), ("n", "comp", "layers")],
    )
    comp = env.make("comp")
    assert comp.state.value == "GATE_FAIL", comp.error
    report = env.report(comp.id)
    assert _check(report, "invention_flags")["status"] == "fail"
    assert {f["id"] for f in report["metrics"]["invention"]["flags"]} == {WALL}
    assert "layer n" in report["metrics"]["invention"]["known"]


@needs_packets
def test_a_comp_over_a_take_keeps_the_base_outside_its_layers(env):
    runs = _comp_graph(env, {"require_accept": False})
    graph = env.client.get(f"/api/v0/projects/{PROJECT}/graph", headers=env.auth).json()
    graph["edges"] = [
        e for e in graph["edges"] if not (e["target"] == "comp" and e["source"] in ("p", "grey"))
    ]
    graph["edges"].append({"id": "grey->comp", "source": "grey", "target": "comp", "targetHandle": "base"})
    r = env.client.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=env.auth)
    assert r.status_code == 200, r.text
    comp = env.make("comp")
    assert comp.state.value == "DONE", [c for c in env.report(comp.id)["checks"] if c["status"] == "fail"]
    assert comp.normalized["base"]["kind"] == "take" and comp.normalized["base"]["run"] == runs["grey"]
    base = env.png(env.run(runs["grey"]).outputs[0])
    out = env.png(comp.outputs[0])
    layered = np.zeros(out.shape[:2], dtype=bool)
    for row in comp.normalized["layers"]:
        layered |= env.png(row["mask_key"])[..., 0] > 0
    assert np.array_equal(out[~layered], base[~layered])
    union = env.png(comp.normalized["mask_union_key"])[..., 0] > 0
    assert np.array_equal(out[~union], _plate16()[~union])  # the base's own chain is inside the union
