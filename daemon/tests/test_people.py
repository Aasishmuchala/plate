"""The People node (T45): the STAFFAGE client against an in-process fake, the packet-Z occlusion maths on analytic
scenes, and whole runs (custody, leak scan, staging, outbound log, the composite, the gate).

The fake (fake_staffage.py) answers the real routes with the real rules; nothing here touches a network."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from fixtures.synthetic import make_depth_scene

from plate_daemon.custody import CustodyViolation
from plate_daemon.egress import LeakBlocked
from plate_daemon.motion import people as P
from plate_daemon.motion.staffage_client import (
    DEFAULT_BASE,
    LoopbackTransport,
    StaffageClient,
    StaffageError,
    StaffageSettings,
    build_person_prompt,
)
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.projects import Project
from plate_engines.adapters.transport import LiveDisabled
from plate_gates.iddiff import outside_mask_check
from plate_pixels import icc
from plate_pixels.exr import read_elements

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROOM_A = REPO / "fixtures" / "packets" / "room_a"
ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
METADATA = {"tEXt", "iTXt", "zTXt", "eXIf", "tIME"}


def _load_fake():
    spec = importlib.util.spec_from_file_location("fake_staffage", HERE / "fake_staffage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_staffage"] = mod
    spec.loader.exec_module(mod)
    return mod


fake_mod = _load_fake()
FakeStaffage = fake_mod.FakeStaffage
PERSON_RGB = np.array(fake_mod.PERSON_COLOUR_RGB, float)


# ---------------------------------------------------------------------------- scenes and helpers


class FakePacket:
    """The packet surface People reads, without an EXR: manifest, root, legend and the editable ids."""

    def __init__(self, camera, *, tier="A", policy=None, ids=()):
        self.root = None
        self.legend = None
        self.manifest = {
            "tier": tier,
            "camera": camera,
            "units": {"system_unit": "meters", "meters_per_unit": 1.0, "up_axis": "Z"},
            "policy": policy or {},
        }
        self._ids = list(ids)

    def editable_ids(self):
        return list(self._ids)


def textured(image: np.ndarray, seed: int = 3) -> np.ndarray:
    """A deterministic grain over a flat synthetic render: registration needs texture on the ring."""
    rng = np.random.default_rng(seed)
    return np.clip(image.astype(np.float32) + rng.normal(0, 7, image.shape), 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def scene():
    return make_depth_scene((512, 512))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return Project.create(root, "tula-wot", {"nda": False})


def nda_project(tmp_path):
    root = tmp_path / "nda-proj"
    root.mkdir()
    return Project.create(root, "tula-wot", {"nda": True})


def node(**params):
    base = {
        "cast": "woman in beige linen",
        "behavior": "walks toward the entrance",
        "placement": {"x": 240, "y": 300},
        "personDepthM": 6.0,
    }
    base.update(params)
    return {"id": "n1", "kind": "People", "params": base}


def client_for(fake) -> StaffageClient:
    return StaffageClient(transport=fake.transport())


def no_sleep(_s):
    return None


def wall_as_background(scene) -> np.ndarray:
    """Planar Z in metres with the wall removed (background +inf): only the box occludes anything."""
    z = scene.z.astype(np.float32).copy()
    z[scene.wall_mask] = np.inf
    return z


def box_mask(scene) -> np.ndarray:
    return scene.labels != 0


def eroded(mask: np.ndarray, px: int = 3) -> np.ndarray:
    return cv2.erode(mask.astype(np.uint8), np.ones((2 * px + 1, 2 * px + 1), np.uint8)).astype(bool)


def rect_matte(shape, x0, y0, x1, y1) -> np.ndarray:
    m = np.zeros(shape[:2], np.float32)
    m[y0:y1, x0:x1] = 1.0
    return m


def u16(img8: np.ndarray) -> np.ndarray:
    return img8.astype(np.uint16) * np.uint16(257)


def person_plate(base16: np.ndarray, matte: np.ndarray) -> np.ndarray:
    p = base16.astype(np.float32)
    p = p * (1 - matte[..., None]) + (PERSON_RGB * 257)[None, None, :] * matte[..., None]
    return np.clip(np.rint(p), 0, 65535).astype(np.uint16)


# ---------------------------------------------------------------------------- the client and the fake


def test_client_walks_every_route_against_the_fake(tmp_path):
    fake = FakeStaffage(tmp_path)
    c = client_for(fake)
    still = tmp_path / "still.png"
    still.write_bytes(bytes(cv2.imencode(".png", np.full((40, 60, 3), 90, np.uint8))[1]))
    film = c.create_film("film-x")
    plate = c.register_plate(
        "plate-x", still, lensMm=24, cameraHeightM=1.5, lightDirection="low sun from the left"
    )
    media = c.ingest_media(still)
    shot = c.add_shot(film["id"], still, media, name="shot-x")
    job = c.start_person(
        film["id"], shot["id"], mode="i2v", stillPath=still, prompt="a person walks", endStillPath=None
    )
    assert plate["lensMm"] == 24 and plate["cameraHeightM"] == 1.5
    assert media["meta"]["width"] == 60 and media["meta"]["height"] == 40
    assert fake.films[film["id"]]["shots"][0]["id"] == shot["id"]
    assert job["status"] == "preparing"
    done = c.wait_for_person(job["id"], poll_s=0, sleep=no_sleep)
    assert (
        done["status"] == "done" and Path(done["platePath"]).is_file() and Path(done["mattePath"]).is_file()
    )
    assert c.accept(job["id"]) == {"ok": True}
    assert c.takes(film["id"])["summary"]["accepted"] == 1
    methods = [(m, p) for m, p, _ in fake.calls]
    assert ("POST", "/api/plates") in methods and ("POST", "/api/media/ingest") in methods
    assert ("PUT", f"/api/films/{film['id']}") in methods


def test_start_person_refuses_what_staffage_would_refuse_before_any_request(tmp_path):
    fake = FakeStaffage(tmp_path)
    c = client_for(fake)
    with pytest.raises(StaffageError, match="needs a chosen still"):
        c.start_person("f", "s", mode="i2v", prompt="p")
    with pytest.raises(StaffageError, match="full prompt"):
        c.start_person("f", "s", mode="wan-edit")
    with pytest.raises(StaffageError, match="mode 'teleport'"):
        c.start_person("f", "s", mode="teleport", prompt="p")
    assert fake.calls == []


def test_staffage_errors_carry_status_and_message(tmp_path):
    fake = FakeStaffage(tmp_path)
    c = client_for(fake)
    with pytest.raises(StaffageError) as nf:
        c.get_person("nope")
    assert nf.value.status == 404 and "job not found" in str(nf.value)
    with pytest.raises(StaffageError) as bad:
        c.register_plate("p", tmp_path.parent / "elsewhere.png")
    assert bad.value.status == 500 and "outside home" in str(bad.value)
    film = c.create_film("f")
    with pytest.raises(StaffageError) as conflict:
        shot_less = {"id": "s1", "name": "s", "meta": None, "layers": []}
        film["shots"] = [shot_less]
        c.put_film(film)
        c.start_person(film["id"], "s1", mode="wan-edit", prompt="p")
    assert conflict.value.status == 409


def _started(fake, home):
    """A client with one film, one ingested shot and one started job on `fake` (files under its `home`)."""
    still = home / "s.png"
    still.write_bytes(bytes(cv2.imencode(".png", np.full((20, 20, 3), 90, np.uint8))[1]))
    c = client_for(fake)
    film = c.create_film("f")
    shot = c.add_shot(film["id"], still, c.ingest_media(still))
    return c, c.start_person(film["id"], shot["id"], mode="i2v", stillPath=still, prompt="p")


def test_wait_for_person_polls_one_id_and_reports_failure_and_timeout(tmp_path):
    fake = FakeStaffage(tmp_path / "a", polls_before_done=3)
    c, job = _started(fake, tmp_path / "a")
    sleeps = []
    done = c.wait_for_person(job["id"], poll_s=7, sleep=sleeps.append)
    assert done["status"] == "done" and sleeps == [7, 7, 7]
    polled = fake.calls_to("GET", f"/api/person/{job['id']}")
    assert len(polled) == 4 and len(fake.calls_to("POST", "/api/person")) == 1  # polled, never resubmitted

    slow = FakeStaffage(tmp_path / "b", polls_before_done=99)
    c2, job2 = _started(slow, tmp_path / "b")
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(StaffageError, match=f"job {job2['id']} still"):
        c2.wait_for_person(job2["id"], poll_s=0, timeout_s=250, sleep=no_sleep, clock=lambda: next(ticks))

    bad = FakeStaffage(tmp_path / "c", fail_with="moderation: refused", polls_before_done=1)
    c3, job3 = _started(bad, tmp_path / "c")
    with pytest.raises(StaffageError, match="moderation: refused"):
        c3.wait_for_person(job3["id"], poll_s=0, sleep=no_sleep)


def test_client_is_loopback_only_and_needs_a_transport_or_live(monkeypatch):
    with pytest.raises(StaffageError, match="loopback"):
        StaffageClient("https://api.atlascloud.ai")
    with pytest.raises(StaffageError, match="loopback"):
        StaffageClient("http://192.168.1.20:3000")
    assert StaffageClient("http://localhost:3000").base == "http://localhost:3000"
    seen = []
    inner = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    with pytest.raises(StaffageError, match="loopback"):
        LoopbackTransport(inner).handle_request(httpx.Request("GET", "https://example.com/x"))
    assert seen == []
    assert (
        LoopbackTransport(inner).handle_request(httpx.Request("GET", "http://127.0.0.1:3000/x")).status_code
        == 200
    )
    monkeypatch.delenv("PLATE_LIVE", raising=False)
    with pytest.raises(LiveDisabled):
        StaffageClient().get_film("x")


def test_settings_route_grade_and_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("PLATE_STAFFAGE_URL", raising=False)
    monkeypatch.delenv("PLATE_STAFFAGE_LOCAL_ONLY", raising=False)
    d = StaffageSettings.load(None)
    assert d.base_url == DEFAULT_BASE and d.local_only is False
    assert d.route()["custody_grade"] == "D" and d.route()["upload_visibility"] == "public"
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"staffage": {"base_url": "http://127.0.0.1:3100", "local_only": True}}),
        encoding="utf-8",
        newline="\n",
    )

    class S:
        config_path = cfg

    s = StaffageSettings.load(S())
    assert s.base_url == "http://127.0.0.1:3100" and s.local_only is True
    r = s.route()
    assert (r["custody_grade"], r["custody_status"], r["upload_visibility"]) == ("C0", "confirmed", "local")
    monkeypatch.setenv("PLATE_STAFFAGE_LOCAL_ONLY", "0")
    monkeypatch.setenv("PLATE_STAFFAGE_URL", "http://127.0.0.1:3200")
    e = StaffageSettings.load(S())
    assert e.local_only is False and e.base_url == "http://127.0.0.1:3200"


def test_person_prompt_is_motion_only_and_keeps_everything_else():
    text = build_person_prompt(
        "one woman in beige linen", "walks to the door", "about 6 m away", "soft daylight"
    )
    assert text.startswith("Add one woman in beige linen, walks to the door, about 6 m away.")
    assert (
        "lit to match the scene: soft daylight" in text
        and "Keep absolutely everything else unchanged" in text
    )
    assert not re.search(r"orbit|dolly|pan |zoom|crane", text, re.I)


# ---------------------------------------------------------------------------- parameters and doctrine


def test_parse_params_reads_the_editor_spelling_and_snake_case():
    p = P.parse_params(
        {
            "data": {
                "kind": "People",
                "params": {
                    "cast": "c1",
                    "behavior": "waits",
                    "placement": {"x": 10, "y": 20},
                    "personDepthM": 3.5,
                    "depthSource": "z",
                    "mode": "wan-edit",
                },
            }
        }
    )
    assert (p.cast, p.behavior, p.foot, p.person_depth_m, p.mode) == (
        "c1",
        "waits",
        (10.0, 20.0),
        3.5,
        "wan-edit",
    )
    q = P.parse_params(
        {"params": {"behavior": "waits", "placement": {"x": 1, "y": 2}, "person_depth_m": 2, "frame": "last"}}
    )
    assert q.person_depth_m == 2.0 and q.frame == "last" and q.mode == "i2v" and q.shadow is True


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"mode": "retake"}, "invented_camera"),
        ({"placement": None}, "placement"),
        ({"placement": {"x": 1}}, "placement"),
        ({"personDepthM": -1}, "bad_depth"),
        ({"personDepthM": "far"}, "bad_depth"),
        ({"behavior": None, "prompt": None}, "params"),
        ({"mode": "teleport"}, "mode"),
        ({"shotId": "s1"}, "params"),
        ({"frame": "soon"}, "params"),
    ],
)
def test_parse_params_refuses(params, code):
    with pytest.raises(P.PeopleError) as e:
        P.parse_params(node(**params))
    assert e.value.code == code


def test_retake_is_refused_before_anything_leaves(tmp_path, project, scene):
    fake = FakeStaffage(tmp_path)
    with pytest.raises(P.PeopleError, match="invented angle"):
        P.run(
            project,
            node(mode="retake"),
            FakePacket(scene.camera),
            u16(scene.image),
            client=client_for(fake),
            depth_m=wall_as_background(scene),
        )
    assert fake.calls == []


# ---------------------------------------------------------------------------- occlusion by packet Z


def _place(scene, depth, *, z=None, matte=None, base=None, **kw):
    base = u16(scene.image) if base is None else base
    matte = rect_matte(base.shape, 100, 120, 420, 300) if matte is None else matte
    z = wall_as_background(scene) if z is None else z
    return matte, P.composite_person(base, person_plate(base, matte), matte, z, depth, shadow=False, **kw)


def test_person_6m_behind_a_box_at_2_5m_is_hidden_in_the_box_and_visible_elsewhere(scene):
    box = box_mask(scene)
    matte, placed = _place(scene, 6.0)
    inside = eroded(box) & (matte > 0.99)
    outside = eroded(~box) & (matte > 0.99)
    assert inside.sum() > 2000 and outside.sum() > 2000
    assert float(placed.alpha[inside].max()) == 0.0, "alpha must be 0 where the box is nearer"
    assert float(placed.alpha[outside].min()) == 1.0, "and 1 where nothing is"
    base = u16(scene.image)
    assert np.array_equal(placed.out16[inside], base[inside])  # the box shows through, bit for bit
    seen = placed.out16[outside].astype(float) / 257
    assert np.abs(seen - PERSON_RGB).max() < 1.0  # opaque person colour
    assert placed.occluded[inside].all() and not placed.occluded[outside].any()


def test_person_between_box_and_wall_is_hidden_by_the_box_only(scene):
    # the real scene: wall at 4.0 m, box front face at 2.5 m, its side face 2.53 to 3.06 m; the person at 3.2 m
    box = box_mask(scene)
    matte, placed = _place(scene, 3.2, z=scene.z.astype(np.float32))
    assert float(placed.alpha[eroded(box) & (matte > 0.99)].max()) == 0.0
    assert float(placed.alpha[eroded(~box) & (matte > 0.99)].min()) == 1.0
    # and a person in front of everything is not occluded at all
    matte2, front = _place(scene, 2.0, z=scene.z.astype(np.float32))
    assert np.allclose(front.alpha, matte2, atol=0)


def test_person_right_at_the_surface_is_tolerated_but_clearly_behind_is_not(scene):
    box = box_mask(scene) & (scene.labels == 1)  # the front face, planar at 2.5 m
    matte = rect_matte((512, 512), 300, 180, 380, 320)
    inside = eroded(box) & (matte > 0.99)
    assert inside.sum() > 500
    _, at_face = _place(scene, 2.52, z=scene.z.astype(np.float32), matte=matte)
    assert float(at_face.alpha[inside].min()) == 1.0  # within the 5 cm tolerance: she stands against the face
    _, behind = _place(scene, 2.62, z=scene.z.astype(np.float32), matte=matte)
    assert float(behind.alpha[inside].max()) == 0.0  # 12 cm behind the face is behind it


def test_background_and_unusable_depth_never_occlude(scene):
    z = np.full((512, 512), np.inf, np.float32)
    matte, placed = _place(scene, 1.0, z=z)
    assert np.array_equal(placed.alpha, matte)
    cam = P.camera_model(scene.camera, 1.0, 512, 512)
    raw = np.full((512, 512), 1e10, np.float32)
    raw[:, :100] = 0.0
    raw[:, 100:200] = np.nan
    raw[:, 200:300] = 3.0
    z = P.depth_metres(raw, {"background_value": 1e10}, cam, 1.0)
    assert np.isinf(z[:, :200]).all() and np.isinf(z[:, 300:]).all() and (z[:, 200:300] == 3.0).all()


def test_scene_units_and_radial_depth_are_converted_to_planar_metres(scene):
    cam = P.camera_model(scene.camera, 0.001, 512, 512)  # millimetre scene
    z_mm = scene.z.astype(np.float32) * 1000.0
    assert np.allclose(P.depth_metres(z_mm, {"depth_kind": "planar"}, cam, 0.001), scene.z, rtol=1e-5)
    xn = (np.arange(512) + 0.5 - cam.cx) / cam.fx
    yn = -((np.arange(512) + 0.5 - cam.cy) / cam.fy)
    radial = (scene.z * np.sqrt(xn[None, :] ** 2 + yn[:, None] ** 2 + 1.0)).astype(np.float32)
    back = P.depth_metres(radial, {"depth_kind": "radial"}, P.camera_model(scene.camera, 1.0, 512, 512), 1.0)
    assert np.allclose(back, scene.z, rtol=1e-4)


def test_composite_changes_only_the_footprint_and_never_the_rest(scene):
    base = u16(textured(scene.image))
    matte = rect_matte((512, 512), 150, 100, 260, 330)
    _, placed = _place(scene, 6.0, base=base, matte=matte)
    changed = np.any(placed.out16 != base, axis=-1)
    assert changed.any() and not (changed & ~placed.footprint).any()
    assert np.array_equal(placed.out16[~placed.footprint], base[~placed.footprint])


# ---------------------------------------------------------------------------- shadow


def test_shadow_darkens_the_ground_and_is_hidden_behind_nearer_geometry(scene):
    base = u16(textured(scene.image))
    box = box_mask(scene)
    ys, xs = np.nonzero(box)
    x_edge = int(xs.min())  # the box's left edge: wall to the left, box to the right
    person = rect_matte((512, 512), x_edge - 90, 180, x_edge - 50, 340)
    shadow_cx, shadow_cy = x_edge + 5, 305  # behind her (above the foot row), straddling wall and box
    layer = np.zeros((512, 512), np.uint8)
    cv2.ellipse(layer, (shadow_cx, shadow_cy), (40, 9), 0, 0, 360, 255, -1)
    dark = (layer > 0)[..., None]
    plate = person_plate(base, person)
    plate = np.where(dark & (person[..., None] == 0), (plate * 0.55).astype(np.uint16), plate)
    z = scene.z.astype(np.float32)  # wall 4.0 m, box 2.5 m and nearer
    placed = P.composite_person(base, plate, person, z, 3.2, foot_y=340.0, shadow=True, reach_px=120)
    in_shadow_on_wall = (layer > 0) & ~box & (person == 0)
    in_shadow_behind_box = (layer > 0) & box & (person == 0)
    assert in_shadow_on_wall.sum() > 100 and in_shadow_behind_box.sum() > 50
    lum_b = base.astype(float).mean(axis=-1)
    lum_o = placed.out16.astype(float).mean(axis=-1)
    core = eroded(in_shadow_on_wall, 4)
    assert core.sum() > 20 and (lum_o[core] / lum_b[core]).mean() < 0.7  # the shadow is kept as a darkening
    assert placed.shadow[core].min() > 0.2
    # behind the box (rows at and above the foot row where the box is nearer than her) the shadow is hidden...
    above_foot = in_shadow_behind_box.copy()
    above_foot[341:] = False
    assert np.array_equal(placed.out16[eroded(above_foot, 2)], base[eroded(above_foot, 2)])
    # ...and nothing outside shadow + person changed
    changed = np.any(placed.out16 != base, axis=-1)
    assert not (changed & ~placed.footprint).any()
    off = P.composite_person(base, plate, person, z, 3.2, shadow=False)
    assert float(off.shadow.max()) == 0.0


# ---------------------------------------------------------------------------- ground plane and person depth


def floor_scene(size=512, *, post=False, step=False):
    """Analytic floor (world Z = 0) under the make_depth_scene camera, a back wall at 8 m, optionally a post at
    3 m (x 40 to 80 percent of the width, rows below the horizon) and a 0.4 m step under part of the frame."""
    ref = make_depth_scene((size, size))
    cam = P.camera_model(ref.camera, 1.0, size, size)
    xn = (np.arange(size) + 0.5 - cam.cx) / cam.fx
    yn = -((np.arange(size) + 0.5 - cam.cy) / cam.fy)
    dz = cam.R[2, 0] * xn[None, :] + cam.R[2, 1] * yn[:, None] - cam.R[2, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        s_floor = (0.0 - cam.t[2]) / dz
        s_step = (0.4 - cam.t[2]) / dz
    z = np.where((dz < 0) & (s_floor > 0) & (s_floor < 8.0), s_floor, 8.0).astype(np.float32)
    if step:
        sel = (dz < 0) & (s_step > 0) & (s_step < 8.0)
        sel[:, : int(size * 0.7)] = False
        sel[: int(size * 0.8)] = False
        z = np.where(sel, s_step, z).astype(np.float32)
    if post:
        z[int(size * 0.25) : int(size * 0.85), int(size * 0.4) : int(size * 0.8)] = 3.0
    return ref, cam, z


def expected_floor_depth(cam, u, v):
    d = cam.ray(u, v)
    return (0.0 - cam.t[2]) / d[2]


def test_ground_plane_is_fitted_from_z_and_back_projects_the_foot(scene):
    ref, cam, z = floor_scene(post=True)
    plane = P.fit_ground_plane(z, cam)
    assert plane is not None and abs(plane.height_m) < 0.01 and plane.share > 0.05
    u, v = 120.0, 470.0
    assert P.ground_depth_at(cam, plane, u, v) == pytest.approx(expected_floor_depth(cam, u, v), rel=0.005)
    notes = []
    params = P.parse_params(node(personDepthM=None, placement={"x": u, "y": v}))
    depth, source, found = P.resolve_person_depth(params, z, cam, notes)
    assert source == "ground_plane" and depth == pytest.approx(expected_floor_depth(cam, u, v), rel=0.005)
    assert found is not None and notes == []


def test_the_node_depth_wins_and_a_step_makes_z_win_over_the_plane():
    ref, cam, z = floor_scene(step=True)
    u, v = 450.0, 470.0  # on the step: Z says 0.4 m up, the floor plane says farther
    params = P.parse_params(node(personDepthM=None, placement={"x": u, "y": v}))
    notes = []
    depth, source, _ = P.resolve_person_depth(params, z, cam, notes)
    assert source == "z_at_foot" and depth == pytest.approx(float(z[int(v), int(u)]), rel=0.01)
    assert any("disagrees with the ground plane" in n for n in notes)
    fixed = P.parse_params(node(personDepthM=4.25, placement={"x": u, "y": v}))
    depth2, source2, _ = P.resolve_person_depth(fixed, z, cam, [])
    assert (depth2, source2) == (4.25, "node")


def test_no_ground_no_z_at_the_foot_is_a_refusal_not_a_guess(scene):
    cam = P.camera_model(scene.camera, 1.0, 512, 512)
    z = np.full((512, 512), np.inf, np.float32)
    params = P.parse_params(node(personDepthM=None, placement={"x": 100, "y": 100}))
    with pytest.raises(P.PeopleError) as e:
        P.resolve_person_depth(params, z, cam, [])
    assert e.value.code == "no_depth"
    with pytest.raises(P.PeopleError) as out:
        P.resolve_person_depth(P.parse_params(node(placement={"x": 900, "y": 10})), z, cam, [])
    assert out.value.code == "placement"


def test_room_a_floor_comes_out_of_the_real_exr_at_height_zero():
    if not ROOM_A.is_dir():
        pytest.skip("fixtures/packets/room_a is absent")
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    z_raw = read_elements(ROOM_A / "beauty.exr", manifest, ["zdepth"])["zdepth"][..., 0]
    cam = P.camera_model(manifest["camera"], 1.0, 640, 360)
    z = P.depth_metres(z_raw, P._zdepth_entry(manifest), cam, 1.0)
    assert np.isinf(z).sum() > 0  # V-Ray's 1e10 background became +inf
    plane = P.fit_ground_plane(z, cam)
    assert plane is not None and abs(plane.height_m) < 0.02  # the fixture room's floor is at world Z 0
    assert P.ground_depth_at(cam, plane, 600.0, 358.0) == pytest.approx(float(z[358, 600]), rel=0.01)


# ---------------------------------------------------------------------------- whole runs


def run_people(project, scene, fake, **kw):
    base = u16(textured(scene.image))
    args = {"client": client_for(fake), "depth_m": wall_as_background(scene), "sleep": no_sleep}
    args.update(kw)
    packet = args.pop("packet", None) or FakePacket(scene.camera)
    n = args.pop("node", None) or node()
    return base, P.run(project, n, packet, base, **args)


def box_straddling_person(scene):
    box = box_mask(scene)
    ys, xs = np.nonzero(box)
    x_edge = int(xs.min())
    return (x_edge - 45, int(ys.min()) + 20, x_edge + 45, int(ys.max()) - 10), x_edge


def test_run_places_a_person_behind_the_box_and_keeps_every_other_pixel(tmp_path, project, scene):
    (x0, y0, x1, y1), x_edge = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1), shadow_box=(x0 - 5, y1 + 4, x0 + 40, y1 + 16))
    base, res = run_people(
        project, scene, fake, node=node(placement={"x": (x0 + x1) // 2, "y": y1}, personDepthM=6.0)
    )
    box = box_mask(scene)
    person = np.zeros((512, 512), bool)
    person[y0:y1, x0:x1] = True
    hidden = eroded(person & box, 4)
    shown = eroded(person & ~box, 4)
    assert hidden.sum() > 500 and shown.sum() > 500
    assert float(res.alpha[hidden].max()) == 0.0 and float(res.alpha[shown].min()) > 0.99
    assert np.array_equal(res.rgb16[hidden], base[hidden])
    assert np.abs(res.rgb16[shown].astype(float) / 257 - PERSON_RGB).max() < 2.0
    # outside the people mask: the approved still, bit for bit, and the gate says so
    check = outside_mask_check(base, res.rgb16, res.region)
    assert check["identical_outside"] and check["n_changed_outside"] == 0 and check["de00_max_outside"] == 0.0
    changed = np.any(res.rgb16 != base, axis=-1)
    assert changed.any() and not (changed & ~res.footprint).any() and res.footprint.sum() < 0.1 * changed.size
    checks = {c.metric_id: c.status for c in res.report.checks}
    # Wave 8 decision 35 (T60): the feather band is advisory on every in-mask edit until calibration.
    assert checks["outside_mask.de00"] == "pass" and checks["feather_band.de00_p99"] == "advisory"
    assert checks["registration.residual_px"] == "pass" and res.registration["registered"]
    assert res.report.engine.id == "staffage" and res.verdict in {"pass", "advisory", "fail"}
    # the shadow STAFFAGE drew survived the composite
    shadow_ring = (res.shadow > 0.2) & ~person
    assert shadow_ring.sum() > 50
    assert res.rgb16[shadow_ring].astype(float).mean() < base[shadow_ring].astype(float).mean() * 0.8
    assert (res.person_depth_m, res.depth_source, res.grade) == (6.0, "node", "D")
    assert res.summary()["occluded_px"] == int(res.occluded.sum()) > 0


def test_run_stages_a_clean_opaque_png_and_logs_staffage_as_the_hop(tmp_path, project, scene):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1))
    base, res = run_people(project, scene, fake, node=node(placement={"x": x0 + 10, "y": y1}))
    # the file STAFFAGE was handed: opaque ULID name under home, sRGB PNG, no metadata chunks, no slug
    staged = res.staged
    assert (
        ULID.match(staged.stem)
        and staged.suffix == ".png"
        and staged.parent == tmp_path / "cache" / "staffage"
    )
    types = set(icc.png_chunk_types(staged.read_bytes()))
    assert types.isdisjoint(METADATA) and "IHDR" in types
    assert fake.received[0]["bytes"] == staged.read_bytes()
    img = cv2.imdecode(np.frombuffer(staged.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.dtype == np.uint8 and img.shape[:2] == (512, 512)  # 8-bit, native size: never upsampled
    assert np.abs(img[..., ::-1].astype(int) - (base // 257).astype(int)).max() <= 1
    # nothing identifying went over the wire
    wire = json.dumps([c[2] for c in fake.calls]).lower()
    assert "tula" not in wire and "proj" not in wire.replace("project", "")
    film = next(iter(fake.films.values()))
    assert ULID.match(film["name"]) and ULID.match(fake.plates[0]["name"])
    assert fake.plates[0]["cameraHeightM"] == pytest.approx(1.5, abs=0.01)
    # the outbound log: one durable pair per request, STAFFAGE as the route, grade D, BLAKE3 of the body
    entries = OutboundLog.for_project(project.root).entries()
    assert len(entries) == len(fake.calls) >= 7
    assert {e["route_id"] for e in entries} == {"staffage"} and {e["custody_grade"] for e in entries} == {"D"}
    assert all(e["status"] == 200 or e["status"] == 201 for e in entries) and all(
        len(e["body_hash"]) == 64 for e in entries
    )
    assert {e["host"] for e in entries} == {"127.0.0.1"} and all(e["run_id"] == res.run_id for e in entries)
    # the CAS holds the sent still, the matte, the person plate and the output
    cas = project.cas()
    assert set(res.assets) == {"sent_still", "person_matte", "person_plate", "output"}
    assert all(cas.exists(k) for k in res.assets.values())


def test_a_big_job_is_polled_by_id_and_a_failed_take_stops_the_run(tmp_path, project, scene):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    slow = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1), polls_before_done=4)
    _, res = run_people(project, scene, slow, node=node(placement={"x": x0 + 5, "y": y1}))
    assert len(slow.calls_to("GET", "/api/person/")) == 5 and len(slow.calls_to("POST", "/api/person")) == 1
    assert res.job["status"] == "done"
    failing = FakeStaffage(tmp_path, fail_with="provider timeout", person_box=(x0, y0, x1, y1))
    with pytest.raises(StaffageError, match="provider timeout"):
        run_people(project, scene, failing, node=node(placement={"x": x0 + 5, "y": y1}))


def test_nda_project_is_refused_before_a_byte_is_staged_or_sent(tmp_path, scene):
    nda = nda_project(tmp_path)
    fake = FakeStaffage(tmp_path)
    with pytest.raises(CustodyViolation) as e:
        run_people(nda, scene, fake)
    assert "staffage" in str(e.value) and e.value.grade is not None and e.value.grade.value == "D"
    assert fake.calls == []
    assert not (tmp_path / "cache" / "staffage").exists()
    assert not (nda.root / "outbound.jsonl").exists()


def test_a_packet_that_floors_custody_below_d_refuses_a_permissive_project(tmp_path, project, scene):
    fake = FakeStaffage(tmp_path)
    packet = FakePacket(scene.camera, policy={"custody": {"nda": False, "allowed_grades": ["C0", "C1"]}})
    with pytest.raises(CustodyViolation):
        run_people(project, scene, fake, packet=packet)
    assert fake.calls == []


def test_local_only_staffage_is_c0_and_an_nda_project_may_use_it(tmp_path, scene):
    nda = nda_project(tmp_path)
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1))
    base, res = run_people(
        nda,
        scene,
        fake,
        settings=StaffageSettings(local_only=True),
        node=node(placement={"x": x0 + 5, "y": y1}),
    )
    assert res.grade == "C0"
    entries = OutboundLog.for_project(nda.root).entries()
    assert entries and {e["custody_grade"] for e in entries} == {"C0"}
    assert outside_mask_check(base, res.rgb16, res.region)["identical_outside"]


def test_the_leak_scan_stops_the_slug_in_the_text_before_any_request(tmp_path, project, scene):
    fake = FakeStaffage(tmp_path)
    with pytest.raises(LeakBlocked, match="(?i)tula"):
        run_people(project, scene, fake, node=node(behavior="walks past the Tula WoT clubhouse"))
    assert fake.calls == []
    # the client scans on its own too (a caller that skipped People.run still cannot leak)
    from plate_daemon.egress import EgressRun

    run = EgressRun.for_project(project, "r1", packet=FakePacket(scene.camera).manifest)
    c = client_for(fake).attach(run=run, route=StaffageSettings().route())
    with pytest.raises(LeakBlocked):
        c.create_film("tula-wot film")
    assert fake.calls == []
    # an absolute path counts by its file name only: the hand-off folder may carry anything, the file name may not
    folder = tmp_path / "tula-wot-folder"
    c.register_plate("p-ok", folder / "x.png")
    with pytest.raises(LeakBlocked):
        c.register_plate("p-bad", folder / "tula-wot.png")
    assert len(fake.calls_to("POST", "/api/plates")) == 1


def test_tier_c_without_depth_is_refused_and_with_depth_it_runs(tmp_path, project, scene):
    fake = FakeStaffage(tmp_path)
    packet = FakePacket(scene.camera, tier="C")
    base = u16(textured(scene.image))
    with pytest.raises(P.PeopleError) as e:
        P.run(project, node(), packet, base, client=client_for(fake), sleep=no_sleep)
    assert e.value.code == "no_depth" and fake.calls == []
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake2 = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1))
    _, res = run_people(project, scene, fake2, packet=packet, node=node(placement={"x": x0 + 5, "y": y1}))
    assert res.report.tier == "C" and res.occluded.any()


def test_a_cast_member_in_an_existing_film_is_passed_by_id_otherwise_the_prompt_is_built(
    tmp_path, project, scene
):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    casts = [{"id": "cast-1", "name": "Meera", "role": "woman in beige linen", "wardrobe": "linen"}]
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1), casts=casts)
    film_id = client_for(fake).create_film("existing")["id"]
    n = node(cast="Meera", filmId=film_id, placement={"x": x0 + 5, "y": y1})
    _, res = run_people(project, scene, fake, node=n)
    body = fake.received[0]["body"]
    assert body["castId"] == "cast-1" and body["behavior"] and "prompt" not in body
    assert "about 6.0 m from the camera" in body["placement"] and body["mode"] == "i2v"
    assert body["stillPath"] == str(res.staged)
    fake2 = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1))
    _, res2 = run_people(
        project, scene, fake2, node=node(cast="a tall man in a grey coat", placement={"x": x0 + 5, "y": y1})
    )
    body2 = fake2.received[0]["body"]
    assert "castId" not in body2 and body2["prompt"].startswith(
        "Add a tall man in a grey coat, walks toward the entrance"
    )
    assert res2.job["mode"] == "i2v"


def test_staffage_output_at_another_size_is_restored_onto_the_plate_grid(tmp_path, project, scene):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1), out_scale=0.5)
    base, res = run_people(project, scene, fake, node=node(placement={"x": x0 + 5, "y": y1}))
    assert res.rgb16.shape == base.shape
    solid = res.alpha > 0.5
    ys, xs = np.nonzero(solid | res.occluded)
    assert abs(xs.min() - x0) <= 4 and abs(xs.max() - (x1 - 1)) <= 4 and abs(ys.min() - y0) <= 4
    assert res.registration["resample"] in {"lanczos4", "area", "none"} and res.registration[
        "result_size"
    ] == [256, 256]
    check = outside_mask_check(base, res.rgb16, res.region)
    assert check["n_changed_outside"] == 0


def test_a_video_take_gives_up_one_frame(tmp_path, project, scene):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1), video=True)
    base, res = run_people(project, scene, fake, node=node(placement={"x": x0 + 5, "y": y1}, frame="mid"))
    assert Path(res.job["platePath"]).suffix == ".mp4" and Path(res.job["mattePath"]).suffix == ".mp4"
    person = np.zeros((512, 512), bool)
    person[y0:y1, x0:x1] = True
    shown = eroded(person & ~box_mask(scene), 6)
    assert shown.sum() > 200
    assert float(res.alpha[shown].min()) > 0.9  # the pha video's frame is the matte
    mean = res.rgb16[shown].astype(float).mean(axis=0) / 257
    assert (
        mean[0] > 180 and mean[0] - mean[1] > 120
    )  # mp4v is lossy and re-tints; she is still the red figure
    assert float(res.alpha[eroded(person & box_mask(scene), 6)].max()) == 0.0
    for kind in ("first", "last", 2):
        again = P.read_layer(Path(res.job["platePath"]), kind)
        assert again.shape[:2] == (512, 512)


def test_run_on_the_real_room_a_packet_uses_its_exr_z_and_its_fitted_floor(tmp_path, project):
    if not ROOM_A.is_dir():
        pytest.skip("fixtures/packets/room_a is absent")
    from plate_daemon.pipeline import PacketView

    pv = PacketView.load("room_a", ROOM_A)
    plate = pv.plate16()
    # the near box spans x 384 to 546 below row 167; she stands on the floor just right of it, straddling its edge
    fake = FakeStaffage(tmp_path, person_box=(515, 150, 585, 357), shadow_box=(520, 358, 560, 359))
    n = node(placement={"x": 550, "y": 357}, personDepthM=None)
    res = P.run(project, n, pv, plate, client=client_for(fake), sleep=no_sleep)
    assert res.depth_source == "ground_plane" and res.person_depth_m == pytest.approx(4.33, abs=0.05)
    assert res.ground_plane is not None and abs(res.ground_plane.height_m) < 0.02
    manifest = pv.manifest
    z_raw = read_elements(ROOM_A / "beauty.exr", manifest, ["zdepth"])["zdepth"][..., 0]
    cam = P.camera_model(manifest["camera"], 1.0, 640, 360)
    z = P.depth_metres(z_raw, P._zdepth_entry(manifest), cam, 1.0)
    expected_hidden = z < (res.person_depth_m - max(0.05, 0.02 * res.person_depth_m))
    person = np.zeros((360, 640), bool)
    person[150:357, 515:585] = True
    hidden = eroded(person & expected_hidden, 4)
    shown = eroded(person & ~expected_hidden, 4)
    assert hidden.sum() > 1000 and shown.sum() > 1000, "the box edge must split her"
    assert float(res.alpha[hidden].max()) == 0.0 and float(res.alpha[shown].min()) > 0.99
    check = outside_mask_check(plate, res.rgb16, res.region)
    assert check["identical_outside"]
    checks = {c.metric_id: c.status for c in res.report.checks}
    assert checks["outside_mask.de00"] == "pass"
    assert "gbuffer_edge_recall_r2" in checks  # the real Tier A gates ran on the composite


def test_accept_takes_the_decision_back_to_staffage(tmp_path, project, scene):
    (x0, y0, x1, y1), _ = box_straddling_person(scene)
    fake = FakeStaffage(tmp_path, person_box=(x0, y0, x1, y1))
    client = client_for(fake)
    _, res = run_people(project, scene, fake, client=client, node=node(placement={"x": x0 + 5, "y": y1}))
    assert P.accept_take(client, res) == {"ok": True}
    assert fake.takes == [{"kind": "decision", "jobId": res.job["id"], "decision": "accepted"}]
