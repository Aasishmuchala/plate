"""CropPair, Move policy and the Kling start/end job (router plan T43).

Everything runs on fakes: the engine is a FakeEngine whose result is a 2 s clip cross-faded (ffmpeg, lossless)
between the start and end stills it was sent, the real KlingStartEnd adapter is only asked to quote, and no
socket is opened. Covered: the pair boxes for the amphi-plaza numbers, no plate upscaling, the Lensbook risk
ladder and its dials, the 409 video lock before any quote, custody, the prompt law, the time map and the video
gate report a finished job records.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from blake3 import blake3

from plate_daemon.adapter import ResultExpired
from plate_daemon.canvas_rules import UNSEEN_GEOMETRY_LABEL
from plate_daemon.cas import CAS
from plate_daemon.custody import CustodyViolation, Grade
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine, SubmitRejected
from plate_daemon.motion import croppair, kling_job, move_policy
from plate_daemon.pipeline import STAGING_KEY, PipelineError
from plate_daemon.quotes import build_quote
from plate_daemon.recipes import RecipeError
from plate_daemon.routes import pipeline_error
from plate_engines.adapters.atlas_video import atlas_video_adapter
from plate_engines.adapters.base import JobStatus
from plate_engines.loader import load_manifests
from plate_gates import io as pio
from plate_gates import thresholds
from plate_gates.router_math import RouterVerdict

ROOT = Path(__file__).resolve().parents[2]
DOLLY = ROOT / "fixtures" / "packets" / "dolly_12f"
needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for the clip tests"
)
ALLOWED_D = {Grade.C0, Grade.C1, Grade.C2, Grade.D}
NDA_GRADES = {Grade.C0, Grade.C1}

# ---------------------------------------------------------------------------- helpers


def textured(w: int, h: int, dtype=np.uint16) -> np.ndarray:
    """A deterministic RGB image with gradients and a checker, so crops of it differ from each other."""
    y, x = np.mgrid[0:h, 0:w]
    r = x / max(w - 1, 1)
    g = y / max(h - 1, 1)
    b = ((x // 16 + y // 16) % 2) * 0.4 + 0.3
    img = np.stack([r, g, b], axis=-1)
    return (img * (65535 if dtype == np.uint16 else 255)).astype(dtype)


def decode_rgb(png: bytes) -> np.ndarray:
    bgr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return np.ascontiguousarray(bgr[..., ::-1])


def png_chunks(png: bytes) -> list[str]:
    out, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        out.append(png[pos + 4 : pos + 8].decode("ascii"))
        pos += 12 + length
    return out


def graph(
    *, gate: str = "pass", approved: bool = True, motion: dict | None = None, edge: bool = True
) -> dict:
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": "pkt_a", "tier": "A"},
        {"id": "e", "kind": "Edit", "gateStatus": gate, "approved": approved},
        {"id": "m", "kind": "Motion", **(motion or {})},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    if edge:
        edges.append({"id": "e->m", "source": "e", "target": "m"})
    return {"nodes": nodes, "edges": edges}


@pytest.fixture(scope="module")
def manifests():
    return load_manifests()


@pytest.fixture
def cas(tmp_path) -> CAS:
    return CAS(tmp_path / "assets")


@pytest.fixture(scope="module")
def still() -> np.ndarray:
    return textured(480, 270)


def make_plan(still, **kw) -> kling_job.MotionPlan:
    kw.setdefault("shot_s", 2)
    kw.setdefault("node_id", "m")
    return kling_job.plan_from_still(still, zoom=(1.0, 1.5), **kw)


class FakeVideoEngine(FakeEngine):
    """A FakeEngine whose result is a clip cross-faded from the start still to the end still it was sent."""

    def __init__(self, *args, tmp: Path, fps: int = 24, seconds: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.tmp, self.fps, self.seconds = Path(tmp), fps, seconds
        self.payloads: list[dict] = []
        self.frames: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.payloads.append(payload)
        ins = payload["inputs"]
        self.frames[job] = tuple(decode_rgb(base64.b64decode(ins[k]["b64"])) for k in ("image", "image_tail"))
        return job

    def fetch(self, result_url):
        super().fetch(result_url)  # call log, expiry
        a, b = (f.astype(np.float64) for f in self.frames[result_url[len(RESULT_SCHEME) :]])
        n = self.fps * self.seconds
        frames = [np.round(a * (1 - t) + b * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
        path = self.tmp / f"clip{self.fps}.mp4"
        pio.write_clip(path, frames, fps=self.fps)
        return path.read_bytes()


# ---------------------------------------------------------------------------- CropPair


def test_pair_boxes_are_exact_for_the_amphi_numbers():
    """relit 5504x3072 at (3633, 2174), 1.5x -> 2.3x: the boxes cut_crops.py recorded for the amphi plaza."""
    big = textured(5504, 3072)
    pair = croppair.make_pair(big, 3633, 2174, 1.5, 2.3, (3840, 2160), quantise="round")
    assert pair["boxes"] == [(1799, 1024, 3668, 2048), (2437, 1507, 2392, 1334)]
    assert pair["push_ratio"] == pytest.approx(2.3 / 1.5)
    assert pair["measured_push_ratio"] == pytest.approx(3668 / 2392)
    # the engine's frame is larger than the end crop: both go at the end crop's native size, nothing is upscaled
    assert pair["size"] == (2392, 1334) and pair["note"] == "native_below_engine_size"
    assert decode_rgb(pair["start_png"]).shape[:2] == (1334, 2392)
    assert decode_rgb(pair["end_png"]).shape[:2] == (1334, 2392)
    assert pair["scale_end"] == 1.0 and pair["scale_start"] == pytest.approx(2392 / 3668)
    assert pair["sizemap"]["start"]["region"] == [1799, 1024, 1799 + 3668, 1024 + 2048]
    assert pair["sizemap"]["end"]["region"] == [2437, 1507, 2437 + 2392, 1507 + 1334]


def test_end_crop_is_the_plate_pixel_for_pixel_when_nothing_caps_it():
    src = textured(640, 360, np.uint8)
    pair = croppair.make_pair(src, 320, 180, 1.0, 1.5, None)
    x0, y0, w, h = pair["boxes"][1]
    assert np.array_equal(decode_rgb(pair["end_png"]), src[y0 : y0 + h, x0 : x0 + w])
    assert decode_rgb(pair["start_png"]).shape == decode_rgb(pair["end_png"]).shape  # one frame for both ends


def test_engine_envelope_shrinks_both_frames_equally(still):
    pair = croppair.make_pair(textured(640, 360), 320, 180, 1.0, 1.5, (320, 180))  # default de00 quantiser
    assert pair["size"] == (320, 180)
    assert decode_rgb(pair["start_png"]).shape[:2] == (180, 320) == decode_rgb(pair["end_png"]).shape[:2]
    assert pair["sizemap"]["start"]["mode"] == "exact"
    assert pair["sizemap"]["start"]["scale"] == pytest.approx(320 / 640)
    assert pair["sizemap"]["end"]["scale"] == pytest.approx(320 / 426)
    assert pair["push_ratio"] == 1.5


@pytest.mark.parametrize("zoom", [(1.0, 1.18), (1.2, 2.0), (1.5, 3.0), (2.4, 2.5)])
@pytest.mark.parametrize("env", [None, (3840, 2160), (1920, 1080), (400, 225)])
def test_the_plate_is_never_upscaled(zoom, env):
    src = textured(640, 360, np.uint8)
    pair = croppair.make_pair(src, 400, 200, *zoom, env)
    w, h = pair["size"]
    for box in pair["boxes"]:
        assert w <= box[2] and h <= box[3]
    if env:
        assert w <= env[0] and h <= env[1]
    assert decode_rgb(pair["start_png"]).shape[:2] == (h, w)


@pytest.mark.parametrize("zoom", [(1.0, 1.0), (0.9, 1.2), (1.2, 3.5), (2.0, 1.5)])
def test_zoom_pair_is_validated(zoom):
    with pytest.raises(ValueError, match="zoom pair"):
        croppair.make_pair(textured(64, 36, np.uint8), 32, 18, *zoom, None)


def test_bad_stills_and_sizes_are_refused():
    ok = textured(64, 36, np.uint8)
    with pytest.raises(croppair.CropPairError, match="H x W x 3"):
        croppair.make_pair(ok[..., 0], 32, 18, 1.0, 1.5, None)
    with pytest.raises(croppair.CropPairError, match="uint8, uint16 or float"):
        croppair.make_pair(ok.astype(np.int32), 32, 18, 1.0, 1.5, None)
    with pytest.raises(croppair.CropPairError, match="positive on both sides"):
        croppair.make_pair(ok, 32, 18, 1.0, 1.5, (320, 0))
    with pytest.raises(croppair.CropPairError, match="quantise"):
        croppair.make_pair(ok, 32, 18, 1.0, 1.5, None, quantise="dither")


def test_frames_are_house_pngs_without_metadata():
    pair = croppair.make_pair(textured(128, 72), 64, 36, 1.0, 1.5, None, quantise="round")
    for png in (pair["start_png"], pair["end_png"]):
        chunks = png_chunks(png)
        assert chunks[0] == "IHDR" and "iCCP" in chunks
        assert not {"tEXt", "zTXt", "iTXt", "tIME", "eXIf"} & set(chunks)
        assert png[24] == 8 and png[25] == 2  # 8-bit RGB


def test_pair_is_stored_in_the_cas_and_the_node_carries_the_boxes(cas):
    pair = croppair.make_pair(textured(128, 72, np.uint8), 64, 36, 1.0, 1.5, None)
    keys = croppair.store_pair(cas, pair)
    assert keys["start"] == blake3(pair["start_png"]).hexdigest()
    assert cas.read_bytes(keys["end"]) == pair["end_png"]
    rec = croppair.node_record(pair, keys)
    json.dumps(rec, allow_nan=False)
    assert rec["boxes"] == [list(b) for b in pair["boxes"]] and rec["push_ratio"] == 1.5
    assert rec["reconform"]["zoompan"] is False and rec["reconform"]["boxes"] == rec["boxes"]


def test_recipe_centres_are_fractions_or_pixels():
    assert croppair.centre_px(1000, 500, [0.5, 0.5]) == (500.0, 250.0)
    assert croppair.centre_px(1000, 500, [3633, 2174]) == (3633.0, 2174.0)
    with pytest.raises(croppair.CropPairError):
        croppair.centre_px(1000, 500, [-1, 0.5])


# ---------------------------------------------------------------------------- Move policy


@pytest.mark.parametrize(
    ("move", "risk", "codes"),
    [
        ("locked_tripod", "none", []),
        ("push_in", "low", ["F6"]),
        ("slider_drift", "low", ["F6"]),
        ("crane_rise", "med", ["A1"]),
        ("light_sweep", "med", ["F12"]),
        ("orbit_le_30", "high", ["A1"]),
        ("aerial_drift", "high", ["A1", "A3"]),
        ("threshold_cross", "highest", ["A1", "F7"]),
    ],
)
def test_the_lensbook_ladder(move, risk, codes):
    v = move_policy.evaluate(move, {})
    assert (v["risk"], v["repair_codes"]) == (risk, codes)
    assert v["blocked"] is (risk in ("high", "highest"))
    assert v["label"] is None and v["reasons"]


def test_orbit_over_thirty_degrees_is_blocked_and_an_override_labels_it():
    v = move_policy.evaluate("orbit_le_30", {"orbit_deg": 35})
    assert v["risk"] == "highest" and v["blocked"] and v["label"] is None
    short = move_policy.evaluate("orbit_le_30", {"orbit_deg": 35, "override": "because"})
    assert short["blocked"] and any("at least 10" in r for r in short["reasons"])
    ok = move_policy.evaluate(
        "orbit_le_30", {"orbit_deg": 35, "override": "client signed off the invented elevation"}
    )
    assert not ok["blocked"] and ok["label"] == UNSEEN_GEOMETRY_LABEL and ok["override"]["accepted"]
    as_list = move_policy.evaluate("aerial_drift", {"override": [{"reason": "the roofscape is the brief"}]})
    assert not as_list["blocked"] and as_list["label"] == UNSEEN_GEOMETRY_LABEL


@pytest.mark.parametrize(
    ("move", "params", "risk"),
    [
        ("push_in", {"reveal_m": 2}, "low"),
        ("push_in", {"reveal_m": 3}, "low"),
        ("push_in", {"reveal_m": 4}, "med"),
        ("push_in", {"reveal_m": 11}, "high"),
        ("crane_rise", {"reveal_m": 11}, "high"),
        ("slider_drift", {"travel_cm": 8}, "low"),
        ("slider_drift", {"travel_cm": 12}, "med"),
        ("light_sweep", {"reveal_m": 20}, "med"),  # the reveal dial belongs to push-in and crane only
    ],
)
def test_dials_only_ever_raise_the_risk(move, params, risk):
    v = move_policy.evaluate(move, params)
    assert v["risk"] == risk and v["blocked"] is (risk == "high")


def test_a_blocking_router_verdict_blocks_regardless_of_override():
    verdict = RouterVerdict(mode="blocking", verdict="render_move", numbers={}, explanation="38 % unseen")
    plain = move_policy.evaluate("push_in", {}, verdict)
    assert plain["blocked"] and any("blocks in blocking mode" in r and "38 %" in r for r in plain["reasons"])
    over = move_policy.evaluate("orbit_le_30", {"override": "client signed off this one"}, verdict)
    assert over["blocked"] and over["label"] is None
    ok = RouterVerdict(mode="blocking", verdict="ai_interpolate", numbers={}, explanation="")
    assert not move_policy.evaluate("push_in", {}, ok)["blocked"]


def test_an_advisory_router_verdict_labels_but_does_not_block():
    v = move_policy.evaluate(
        "push_in", {}, {"mode": "advisory", "verdict": "render_move", "explanation": "x"}
    )
    assert not v["blocked"] and v["label"] == UNSEEN_GEOMETRY_LABEL and v["router"]["mode"] == "advisory"


def test_policy_input_errors():
    assert move_policy.evaluate("Push-in", {})["move_kind"] == "push_in"
    assert move_policy.moves()[0] == "locked_tripod"
    with pytest.raises(RecipeError, match="unknown move"):
        move_policy.evaluate("whip_pan", {})
    with pytest.raises(ValueError, match="negative"):
        move_policy.evaluate("push_in", {"reveal_m": -1})
    with pytest.raises(ValueError, match="finite"):
        move_policy.evaluate("orbit_le_30", {"orbit_deg": float("nan")})
    with pytest.raises(ValueError, match="mode"):
        move_policy.evaluate("push_in", {}, {"mode": "loud", "verdict": "render_move"})


# ---------------------------------------------------------------------------- plan: prompt law, duration, time map


def test_the_prompt_law_strips_pace_words_under_an_end_pin(still):
    plan = make_plan(still, prompt="A slow, gentle push-in. Move slowly and leisurely to the door, gently.")
    low = plan.prompt.lower()
    assert not any(w in low for w in ("slow", "gently", "leisurely"))
    assert plan.law["pace_words_stripped"] is True and plan.law["end_pinned"] is True
    default = make_plan(still)  # the croppair_pushin recipe
    assert default.recipe_id == "croppair_pushin" and default.law["pace_words_stripped"] is False
    assert default.law["tokens_unfilled"] == 0 and "constant speed" in default.prompt
    assert len(default.negative_prompt.split()) < 30
    with pytest.raises(PipelineError) as e:
        make_plan(still, prompt="slowly")
    assert e.value.code == "prompt_empty"


def test_negative_prompt_stays_under_thirty_words(still):
    assert make_plan(still, negative_prompt=" ".join(["drift"] * 29)).negative_prompt.count("drift") == 29
    with pytest.raises(PipelineError) as e:
        make_plan(still, negative_prompt=" ".join(["drift"] * 30))
    assert e.value.code == "negative_prompt_long" and e.value.status == 422


@pytest.mark.parametrize(("shot", "seconds"), [(0.2, 1), (2, 2), (4.2, 5), (14.01, 15), (40, 15)])
def test_duration_is_ceil_of_the_shot_clamped_to_one_to_fifteen(still, shot, seconds):
    plan = make_plan(still, shot_s=shot)
    assert plan.duration_s == seconds and plan.params()["duration"] == seconds


def test_engine_resolution_and_kind(still):
    assert make_plan(still).params()["resolution"] == "4K"
    v3 = make_plan(still, engine_id="kling-v3-pro")
    assert v3.params()["resolution"] == "1080P" and v3.engine_id == "kling-v3-pro"
    with pytest.raises(PipelineError, match="not a Kling"):
        make_plan(still, engine_id="seedance-2.5-i2v")
    with pytest.raises(PipelineError, match="resolution"):
        make_plan(still, engine_id="kling-v3-pro", resolution="4K")
    assert make_plan(still, seed=7).params()["seed"] == 7


def test_virtual_time_map_for_a_still_only_crop_pair(still):
    plan = make_plan(still, shot_s=2)
    tm = plan.timemap.to_json()
    assert plan.timemap_source == "virtual"
    assert (tm["packet_fps"], tm["engine_fps"]) == ("24/1", "24/1")
    assert tm["packet_frames"] == [0, 47] and tm["duration_s"] == 2.0
    assert make_plan(still, output_fps=30).timemap.to_json()["engine_fps"] == "30/1"


@pytest.mark.skipif(not (DOLLY / "manifest.json").is_file(), reason="fixtures/packets/dolly_12f missing")
def test_packet_time_map_maps_the_scene_rate_to_the_engine_rate(still):
    plan = make_plan(still, packet_motion=DOLLY)
    tm = plan.timemap.to_json()
    assert plan.timemap_source == "packet"
    assert (tm["packet_fps"], tm["engine_fps"]) == ("25/1", "24/1") and tm["packet_frames"] == [0, 11]
    assert plan.timemap.packet_frame_for(0) == 0.0


# ---------------------------------------------------------------------------- the move policy meets the plan


def test_a_blocked_move_never_becomes_a_plan(still):
    with pytest.raises(kling_job.MotionBlocked) as e:
        make_plan(still, move_kind="orbit_le_30", move_params={"orbit_deg": 35})
    assert (e.value.status, e.value.code) == (409, "MOVE_BLOCKED") and e.value.extra["move"][
        "risk"
    ] == "highest"
    plan = make_plan(
        still,
        move_kind="orbit_le_30",
        move_params={"orbit_deg": 35, "override": "client signed off the invented elevation"},
    )
    assert plan.move["label"] == UNSEEN_GEOMETRY_LABEL and plan.record()["move"]["risk"] == "highest"
    blocking = RouterVerdict(mode="blocking", verdict="render_move", numbers={}, explanation="")
    with pytest.raises(kling_job.MotionBlocked):
        make_plan(still, move_params={"override": "client signed off this one"}, router_verdict=blocking)
    assert make_plan(still, move_kind="push_in", move_params={"reveal_m": 2}).move["risk"] == "low"


# ---------------------------------------------------------------------------- the video lock and custody


def test_a_locked_video_node_answers_409_before_any_quote(still, manifests, monkeypatch):
    plan = make_plan(still)

    def boom(*a, **k):
        raise AssertionError("a locked node must not reach routing or pricing")

    monkeypatch.setattr(kling_job, "build_quote", boom)
    monkeypatch.setattr(kling_job, "pick_route", boom)
    for g in (graph(gate="none"), graph(gate="fail"), graph(approved=False), graph(edge=False)):
        with pytest.raises(kling_job.MotionLocked) as e:
            kling_job.quote_motion(plan, g, manifests, ALLOWED_D)
        assert (e.value.status, e.value.code) == (409, "VIDEO_LOCKED")
    api = pipeline_error(kling_job.MotionLocked("locked"))
    assert api.status == 409 and api.body["error"] == "VIDEO_LOCKED"


def test_an_unlocked_node_is_quoted_per_second_on_the_allowed_route(still, manifests):
    plan = make_plan(still, shot_s=4.2)
    route, quote = kling_job.quote_motion(plan, graph(), manifests, ALLOWED_D)
    assert (
        route["engine_id"] == "kling-o3-4k" and route["provider"] == "atlas" and route["custody_grade"] == "D"
    )
    assert quote.model == "kling-o3-4k" and quote.custody_grade == "D"
    assert quote.price_usd == pytest.approx(5 * 0.357) and quote.price_status == "unconfirmed"
    assert quote.size["label"] == "4K"
    v3 = make_plan(still, shot_s=4.2, engine_id="kling-v3-pro", node_id="m")
    assert kling_job.quote_motion(v3, graph(), manifests, ALLOWED_D)[1].price_usd == pytest.approx(5 * 0.153)


def test_custody_refuses_nda_projects_after_the_lock(still, manifests):
    plan = make_plan(still)
    with pytest.raises(CustodyViolation):
        kling_job.quote_motion(plan, graph(), manifests, NDA_GRADES)


def test_other_canvas_rule_violations_are_422_with_the_rules_code(still, manifests):
    plan = make_plan(still)
    with pytest.raises(PipelineError) as e:
        kling_job.quote_motion(plan, graph(motion={"riskLabel": "high"}), manifests, ALLOWED_D)
    assert (e.value.status, e.value.code) == (422, "HIGH_RISK_MOVE")
    with pytest.raises(PipelineError) as e:
        kling_job.check_unlocked(graph(), "ghost")
    assert e.value.status == 404
    with pytest.raises(PipelineError) as e:
        kling_job.check_unlocked(graph(), "e")
    assert e.value.code == "node_kind_unsupported"
    kling_job.check_unlocked(graph(), "m")  # an unlocked node passes silently


def test_the_plan_becomes_a_run_spec_without_the_image_staging_block(still, manifests):
    plan = make_plan(still, shot_s=3, seed=11)
    route, quote = kling_job.quote_motion(plan, graph(), manifests, ALLOWED_D)
    spec = kling_job.to_run_create(plan, "plate-fixture", "pkt_a", route)
    assert spec.node_id == "m" and STAGING_KEY not in spec.inputs
    assert spec.inputs["image"] == plan.start_hash and spec.inputs["image_tail"] == plan.end_hash
    assert spec.params == {
        "duration": 3,
        "negative_prompt": plan.negative_prompt,
        "resolution": "4K",
        "seed": 11,
    }
    assert spec.inputs["motion"]["task"] == "crop_pair_motion" and spec.inputs["motion"]["boxes"]
    again = build_quote(manifests["kling-o3-4k"], route, spec.inputs, spec.params)
    assert again.price_usd == pytest.approx(quote.price_usd)
    assert plan.idempotency_key(route) == plan.idempotency_key(route)
    assert plan.idempotency_key(route, attempt=2) != plan.idempotency_key(route)
    assert make_plan(still, shot_s=3, seed=12).idempotency_key(route) != plan.idempotency_key(route)


def test_the_real_kling_adapter_takes_the_plan_inputs(still, manifests, tmp_path):
    plan = make_plan(still, shot_s=3)

    def refuse(request):  # a quote is manifest data only: any request is a bug
        raise AssertionError(f"unexpected request {request.url}")

    ad = atlas_video_adapter(manifests["kling-o3-4k"], None, httpx.MockTransport(refuse))
    inputs = plan.adapter_inputs(tmp_path / "frames")
    q = ad.quote(inputs, plan.params())
    assert q.price_usd == pytest.approx(3 * 0.357) and q.custody_grade == "D"
    assert (q.size.w, q.size.h) == (3840, 2160)
    assert inputs["image"]["path"] != inputs["image_tail"]["path"]
    assert all(len(Path(inputs[k]["path"]).stem) == 26 for k in ("image", "image_tail"))  # opaque names


# ---------------------------------------------------------------------------- running the job


@needs_ffmpeg
def test_a_finished_job_records_the_time_map_and_a_video_gate_report(still, cas, tmp_path):
    plan = make_plan(still)
    engine = FakeVideoEngine(tmp=tmp_path)
    result = kling_job.run_motion(plan, engine, cas, sleep=lambda s: None)
    report = result.gate_report

    first, last = thresholds.load().get("video.first_frame"), thresholds.load().get("video.last_frame")
    assert report["metrics"]["landing"]["first"] <= first.value
    assert report["metrics"]["landing"]["last"] <= last.value
    statuses = {c["metric_id"]: c["status"] for c in report["checks"]}
    assert statuses["video.first_frame"] == "pass" and statuses["video.last_frame"] == "pass"
    assert statuses["video.duration_s"] == "pass" and result.verdict != "fail"
    assert "video.camera_lock_median_flow_px" not in report["metrics"]  # a push-in moves by design
    assert report["engine"]["id"] == "kling-o3-4k"
    assert report["metrics"]["output"]["fps"] == "24/1" and report["metrics"]["output"]["nb_frames"] == 48

    tm = result.timemap.to_json()
    assert tm == report["metrics"]["timemap"] == plan.timemap.to_json()
    assert (tm["packet_fps"], tm["engine_fps"], tm["method"]) == ("24/1", "24/1", "nearest")

    # exactly one submit; the start under `image`, the end under `image_tail`, opaque names, nothing else
    assert engine.job_count == 1 and len(engine.payloads) == 1
    ins = engine.payloads[0]["inputs"]
    assert base64.b64decode(ins["image"]["b64"]) == plan.start_png
    assert base64.b64decode(ins["image_tail"]["b64"]) == plan.end_png
    names = {ins["image"]["name"], ins["image_tail"]["name"]}
    assert len(names) == 2 and all(
        re.fullmatch(r"[0-9a-f]{26}\.png", n) for n in names
    )  # opaque, no node name
    sent = engine.payloads[0]["params"]
    assert sent["duration"] == 2 and len(sent["negative_prompt"].split()) < 30

    assert cas.exists(result.video_key) and cas.exists(result.start_key) and cas.exists(result.end_key)
    assert result.video_path.suffix == ".mp4" and result.idempotency_key
    rec = result.record(plan)
    json.dumps(rec, allow_nan=False)
    assert rec["video"] == result.video_key and rec["gate"]["verdict"] == result.verdict
    assert rec["boxes"] == [list(b) for b in plan.pair["boxes"]] and rec["task"] == "crop_pair_motion"


@needs_ffmpeg
def test_a_wrong_end_frame_fails_the_landing_gate(still, cas, tmp_path):
    """The engine ignored the end pin: the clip ends on the start frame. The report must say so."""

    class IgnoresEnd(FakeVideoEngine):
        def submit(self, payload, idempotency_key):
            job = super().submit(payload, idempotency_key)
            a, _ = self.frames[job]
            self.frames[job] = (a, a)
            return job

    result = kling_job.run_motion(make_plan(still), IgnoresEnd(tmp=tmp_path), cas, sleep=lambda s: None)
    assert result.gate_report["metrics"]["landing"]["last"] > thresholds.load().get("video.last_frame").value
    statuses = {c["metric_id"]: c["status"] for c in result.gate_report["checks"]}
    assert statuses["video.last_frame"] != "pass"


@needs_ffmpeg
def test_a_clip_at_another_frame_rate_rebuilds_the_time_map(still, cas, tmp_path):
    plan = make_plan(still, shot_s=2)
    result = kling_job.run_motion(plan, FakeVideoEngine(tmp=tmp_path, fps=30), cas, sleep=lambda s: None)
    assert result.timemap.to_json()["engine_fps"] == "30/1"
    assert result.gate_report["metrics"]["timemap"]["engine_fps"] == "30/1"
    assert any("not the assumed 24/1" in n for n in result.notes)
    assert plan.timemap.to_json()["engine_fps"] == "24/1"  # the plan itself is untouched


@needs_ffmpeg
def test_resubmitting_the_same_plan_reuses_the_provider_job(still, cas, tmp_path):
    plan = make_plan(still)
    engine = FakeVideoEngine(tmp=tmp_path)
    a = kling_job.run_motion(plan, engine, cas, sleep=lambda s: None)
    b = kling_job.run_motion(plan, engine, cas, sleep=lambda s: None)
    assert a.idempotency_key == b.idempotency_key and engine.job_count == 1
    c = kling_job.run_motion(plan, engine, cas, attempt=2, sleep=lambda s: None)
    assert c.idempotency_key != a.idempotency_key and engine.job_count == 2


@needs_ffmpeg
def test_refusals_failures_timeouts_and_expired_results(still, cas, tmp_path):
    plan = make_plan(still)
    with pytest.raises(kling_job.MotionJobFailed) as e:
        kling_job.run_motion(
            plan, FakeVideoEngine(tmp=tmp_path, behaviour="refuse"), cas, sleep=lambda s: None
        )
    assert e.value.state == "refused" and "moderation" in str(e.value)

    ticks = iter(range(0, 10_000, 1000))
    with pytest.raises(kling_job.MotionJobFailed) as e:
        kling_job.run_motion(
            plan,
            FakeVideoEngine(tmp=tmp_path, behaviour="slow"),
            cas,
            sleep=lambda s: None,
            now=lambda: float(next(ticks)),
            timeout_s=5.0,
        )
    assert e.value.state == "timeout"

    with pytest.raises(SubmitRejected):
        kling_job.run_motion(
            plan, FakeVideoEngine(tmp=tmp_path, behaviour="fail_submit"), cas, sleep=lambda s: None
        )
    with pytest.raises(ResultExpired):
        kling_job.run_motion(
            plan, FakeVideoEngine(tmp=tmp_path, behaviour="expire_url"), cas, sleep=lambda s: None
        )


@needs_ffmpeg
def test_locked_tripod_scores_the_camera_lock(still, cas, tmp_path):
    plan = make_plan(still, move_kind="locked_tripod")
    result = kling_job.run_motion(plan, FakeVideoEngine(tmp=tmp_path), cas, sleep=lambda s: None)
    assert "video.camera_lock_median_flow_px" in result.gate_report["metrics"]


# ---------------------------------------------------------------------------- a plate_engines adapter behind the worker protocol


class StubKling:
    """The slice of KlingStartEnd that AdapterEngine touches, serving a fixed clip."""

    engine_id = "kling-o3-4k"

    def __init__(self, clip: bytes, fps: float | None = None):
        self.clip, self.fps, self.staged, self.polls = clip, fps, None, 0

    def validate_params(self, params):
        return {"cfg_scale": 0.5, **params}

    def submit(self, staged):
        self.staged = staged
        return "pred_1"

    def poll(self, job):
        self.polls += 1
        if self.polls < 2:
            return JobStatus(state="running")
        return JobStatus(state="done", outputs=["https://static.atlascloud.ai/clip.mp4"], output_fps=self.fps)

    def fetch(self, url):
        assert url.startswith("https://static.atlascloud.ai/")
        return self.clip


@needs_ffmpeg
def test_adapter_engine_runs_a_plate_engines_adapter_through_the_job(still, cas, tmp_path):
    plan = make_plan(still)
    feed = FakeVideoEngine(tmp=tmp_path)  # only to render the clip the stub will serve
    job = feed.submit(plan.payload(), "k")
    clip = feed.fetch(f"{RESULT_SCHEME}{job}")
    stub = StubKling(clip, fps=24.0)
    engine = kling_job.AdapterEngine(stub, tmp_path / "staging")
    result = kling_job.run_motion(plan, engine, cas, sleep=lambda s: None)
    assert result.verdict != "fail" and engine.output_fps == 24.0 and stub.polls == 2

    staged = stub.staged
    assert staged.inputs["prompt"] == plan.prompt and staged.params["duration"] == 2
    assert (staged.size.w, staged.size.h) == plan.size
    for name, png in (("image", plan.start_png), ("image_tail", plan.end_png)):
        ref = staged.inputs[name]
        assert Path(ref["path"]).read_bytes() == png and ref["role"] == "plate"
        assert Path(ref["path"]).name == plan.payload()["inputs"][name]["name"]
