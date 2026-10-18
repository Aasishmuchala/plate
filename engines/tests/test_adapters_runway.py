"""Contract tests for the Runway Dev API video adapter (router plan T118, Wave 11 decisions 59 to 70).

Every request runs against the synthetic recordings in contract/runway (authored from the researched schemas, not
captured), so a body the recordings do not cover raises UnrecordedRequest: each test pins the exact bytes the adapter
sends, and also asserts the body it expects. conftest.py blocks sockets.

Checked here: the closed per-model field allowlists, the ratio that follows the frame (an off-ratio frame is refused,
never centre-cropped), refuse-or-fold for what a model cannot express, one create POST and no retry, the 5 s poll
rule, the failureCode mapping, uploads without the Runway headers, and that the key never leaves the header.

Until T119 ships the `runway` routes, the route is built here from the decision 62 / T119 shape and appended to the
shipped manifest (`with_runway`); once the manifests carry it, theirs is used.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from plate_engines.adapters import runway_schema
from plate_engines.adapters.atlas_video import VideoSubmitError, refusal_from_job_id
from plate_engines.adapters.base import FetchError, InputError, MissingTransport, ParamError, Staged
from plate_engines.adapters.runway_video import RunwayVideo
from plate_engines.adapters.transport import (
    CONTRACT_DIR,
    FixtureTransport,
    HostNotAllowed,
    UnrecordedRequest,
    body_key,
)
from plate_engines.models import EngineManifest, EngineSize, Route

RUNWAY_DIR = CONTRACT_DIR / "runway"
KEY = "key_" + "0123456789abcdef" * 8  # the documented key shape, not a real key
API = "https://api.dev.runwayml.com/v1"
TASK = "0a6a3a0e-5a1e-4d7a-9f49-2b6f0a4c2d11"
VEO_TASK = "5b0e8c1f-2d34-4a6b-8c9d-0e1f2a3b4c5d"
DRAFT_TASK = "a1b2c3d4-e5f6-4789-8abc-def012345678"
ENHANCED_TASK = "9e913fa2-b3c4-45d6-87e8-f90a1b2c3d4e"
SAFETY_TASK = "1c1f5e2a-3b4c-4d5e-8f60-718293a4b5c6"
BAD_OUTPUT_TASK = "2d2a6f3b-4c5d-4e6f-9071-8293a4b5c6d7"
THIRD_PARTY_TASK = "3e3b7a4c-5d6e-4f70-a182-93a4b5c6d7e8"
ASSET_TASK = "4f4c8b5d-6e7f-4081-b293-a4b5c6d7e8f9"
CANCELLED_TASK = "5a5d9c6e-7f80-4192-83a4-b5c6d7e8f90a"
MISSING_TASK = "6b6e0d7f-8091-42a3-94b5-c6d7e8f90a1b"
BUSY_TASK = "7c7f1e80-91a2-43b4-a5c6-d7e8f90a1b2c"
GONE_TASK = "8d802f91-a2b3-44c5-b6d7-e8f90a1b2c3d"
OUTPUT_URL = "https://dnznrvs05pmza.cloudfront.net/output.mp4?_jwt=test"
UPLOAD_URL = "https://plate-test-uploads.s3.amazonaws.com/"
OFFLIST_UPLOAD_URL = "https://uploads.example.net/"
CLAY_URI = "runway://ephemeral/clay-test"
BIG_URI = "runway://ephemeral/big-start"
RUNWAY_HOSTS = ["api.dev.runwayml.com", ".cloudfront.net", ".amazonaws.com"]
SLUGS = {
    "seedance-2.5-i2v": "seedance2_5",
    "seedance-2.5-r2v": "seedance2_5",
    "kling-o3-4k": "klingO3_4k",
    "kling-v3-pro": "kling3.0_pro",
    "veo-3.1": "veo3.1",
    "gen-4.5": "gen4.5",
}
ROUTE_USD = {
    "seedance-2.5-i2v": 0.20,
    "seedance-2.5-r2v": 0.20,
    "kling-o3-4k": 0.43,
    "kling-v3-pro": 0.12,
    "veo-3.1": 0.20,
    "gen-4.5": 0.12,
}

O3_PROMPT = "Push in to the dining table; the light shifts to dusk."
O3_NEGATIVE = "morphing, warping, extra people"
V3_PROMPT = "Dolly toward the balcony door."
VEO_PROMPT = "Camera holds; steam rises from the cup."
VEO_NEGATIVE = "blur, text"
SEEDANCE_PROMPT = "Camera holds still; late sun moves across the oak floor."
R2V_PROMPT = "Wide establishing view of the lobby."
GEN_PROMPT = "Dolly forward through the atrium."
ELEMENTS_PROMPT = "The sofa, lamp and rug stay in place as the light warms."
UPLOAD_PROMPT = "Hold on the facade as the clouds pass."
PROBES = {  # one kling-v3-pro body per unhappy create answer
    "safety": "Safety probe: the doors open.",
    "issues": "Issues probe: the doors open.",
    "429": "Rate probe: the doors open.",
    "503": "Busy probe: the doors open.",
    "401": "Key probe: the doors open.",
}


# ---- deterministic bytes: what the recordings were authored against -------------------------------------


def png_bytes(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """A solid-colour RGB PNG with a stored deflate block (the test_adapters_video helper): byte-identical everywhere."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    assert len(raw) < 65535

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    stored = (
        b"\x78\x01\x01"
        + struct.pack("<HH", len(raw), len(raw) ^ 0xFFFF)
        + raw
        + struct.pack(">I", zlib.adler32(raw))
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", stored)
        + chunk(b"IEND", b"")
    )


def big_png(payload: int = 4_000_000) -> bytes:
    """A PNG-headed 1920x1080 file of about 4 MB (zero-filled IDAT): too big for a data URI, never decoded."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1920, 1080, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", b"\x00" * payload)
        + chunk(b"IEND", b"")
    )


FTYP = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
STILL_A = png_bytes(16, 9, (200, 40, 40))
STILL_B = png_bytes(16, 9, (40, 40, 200))
STILL_END = png_bytes(16, 9, (240, 240, 240))
RESULT = FTYP + struct.pack(">I", 24) + b"free" + b"PLATE-RUNWAY-K03"  # the 48-byte result stub
CLAY = FTYP + b"PLATE-RUNWAY-CLAY" * 4
CLAY_OFFLIST = FTYP + b"PLATE-RUNWAY-CLAY-OFFLIST" * 4
CLAY_INIT_429 = FTYP + b"PLATE-RUNWAY-CLAY-INIT-429" * 4
CLAY_PUT_403 = FTYP + b"PLATE-RUNWAY-CLAY-PUT-403" * 4


def data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def opaque_name(data: bytes, ext: str) -> str:
    from blake3 import blake3

    return f"{blake3(data).hexdigest()[:26]}.{ext}"


def multipart_key(fields: dict[str, str], filename: str, data: bytes, mime: str) -> str:
    """The body_hash FixtureTransport computes for the upload hop (boundary-free)."""
    req = httpx.Request("POST", UPLOAD_URL, data=fields, files={"file": (filename, data, mime)})
    req.read()
    return body_key(req.headers["content-type"], req.content)


def upload_fields(key: str) -> dict[str, str]:
    return {"key": key, "policy": "cG9saWN5", "x-amz-signature": "sig"}


# ---- the bodies the recordings pin -------------------------------------------------------------------------


def first(data: bytes) -> dict:
    return {"uri": data_uri(data), "position": "first"}


def last(data: bytes) -> dict:
    return {"uri": data_uri(data), "position": "last"}


def body_kling_o3() -> dict:
    return {
        "model": "klingO3_4k",
        "promptImage": [first(STILL_A), last(STILL_END)],
        "promptText": f"{O3_PROMPT}\nAvoid: {O3_NEGATIVE}",
        "ratio": "3840:2160",
        "duration": 5,
        "audio": False,
    }


def body_kling_v3(prompt: str = V3_PROMPT, start: str | None = None) -> dict:
    return {
        "model": "kling3.0_pro",
        "promptImage": [{"uri": start or data_uri(STILL_A), "position": "first"}],
        "promptText": prompt,
        "ratio": "1920:1080",
        "duration": 5,
        "audio": False,
        "cfgScale": 0.5,
    }


def body_veo() -> dict:
    return {
        "model": "veo3.1",
        "promptImage": [first(STILL_A), last(STILL_END)],
        "promptText": VEO_PROMPT,
        "ratio": "1920:1080",
        "duration": 6,
        "audio": False,
        "seed": 1234,
        "negativePrompt": VEO_NEGATIVE,
    }


def body_seedance_i2v() -> dict:
    return {
        "model": "seedance2_5",
        "promptImage": [first(STILL_A)],
        "promptText": SEEDANCE_PROMPT,
        "ratio": "1920:1080",
        "duration": 5,
        "audio": False,
        "seed": 7,
    }


def body_seedance_draft() -> dict:
    return {
        "model": "seedance2_5",
        "promptImage": [first(STILL_A)],
        "promptText": SEEDANCE_PROMPT,
        "ratio": "854:480",
        "duration": 4,
        "audio": False,
        "draft": True,
    }


def body_seedance_r2v() -> dict:
    return {
        "model": "seedance2_5",
        "promptText": R2V_PROMPT,
        "ratio": "854:480",
        "duration": 8,
        "audio": False,
        "draft": True,
        "references": [{"uri": data_uri(STILL_A)}, {"uri": data_uri(STILL_B)}],
        "referenceVideos": [{"type": "video", "uri": CLAY_URI}],
    }


def body_gen45() -> dict:
    return {
        "model": "gen4.5",
        "promptImage": [first(STILL_A)],
        "promptText": GEN_PROMPT,
        "ratio": "1280:720",
        "duration": 5,
    }


def body_elements() -> dict:
    return {
        "model": "klingO3_4k",
        "promptImage": [first(STILL_A), last(STILL_END)],
        "promptText": ELEMENTS_PROMPT,
        "ratio": "3840:2160",
        "duration": 5,
        "audio": False,
        "references": [{"uri": data_uri(STILL_B)}] * 3,
    }


# ---- harness -----------------------------------------------------------------------------------------------


class Clock:
    """An injected monotonic clock; sleep() advances it (VideoAdapter.wait never really sleeps here)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RUNWAYML_API_SECRET", KEY)
    monkeypatch.delenv("PLATE_LIVE", raising=False)


def runway_route(engine: str) -> Route:
    """The route T119 appends (decision 62): D on the evidence, private uploads, the catalogue price."""
    return Route.model_validate(
        {
            "provider": "runway",
            "endpoint": "https://api.dev.runwayml.com/v1",
            "region": "us",
            "custody_grade": "D",
            "custody_status": "confirmed",
            "terms_hash": "unknown",
            "upload_visibility": "private",
            "model_slug": SLUGS[engine],
            "allowed_hosts": RUNWAY_HOSTS,
            "max_inflight": 1,
            "price": {
                "model": "per_second",
                "usd": ROUTE_USD[engine],
                "status": "confirmed",
                "source": "docs.dev.runwayml.com/guides/pricing.md (test copy of the T119 route)",
                "as_of": "2026-10-18",
            },
        }
    )


GEN45 = {
    "id": "gen-4.5",
    "name": "Runway Gen-4.5",
    "version": "unpinned",
    "pinned": False,
    "kind": "video_i2v",
    "model_id": "gen4.5",
    "flags": ["runway_hosts_unverified"],
    "inputs": [
        {"name": "image", "type": "image", "required": True},
        {"name": "prompt", "type": "text", "required": True},
    ],
    "outputs": [{"name": "video", "type": "video", "required": True}],
    "params": [
        {"name": "duration", "type": "int", "default": 5, "range": [2, 10]},
        {
            "name": "ratio",
            "type": "enum",
            "default": "16:9",
            "enum": ["16:9", "9:16", "4:3", "1:1", "3:4", "21:9"],
        },
        {"name": "seed", "type": "int", "range": [0, 4294967295], "advanced": True},
    ],
    "price": {
        "model": "per_second",
        "usd": 0.12,
        "status": "unconfirmed",
        "source": "test copy of the T119 manifest",
        "as_of": "2026-10-18",
    },
    "determinism": "nondeterministic",
    "licence": {"vendor_terms_url": "https://runway.com/terms-of-use", "training_opt_out": False},
    "allowed_tasks": ["crop_pair_motion"],
}


def with_runway(manifests, engine: str) -> EngineManifest:
    """The shipped manifest with its runway route (T119's once it lands, this module's copy until then)."""
    if engine == "gen-4.5" and engine not in manifests:
        return EngineManifest.model_validate({**GEN45, "routes": [runway_route(engine).model_dump()]})
    m = manifests[engine]
    if any(r.provider == "runway" for r in m.routes):
        return m
    return m.model_copy(update={"routes": [*m.routes, runway_route(engine)]})


def make(manifests, engine: str, *, now=None) -> tuple[RunwayVideo, FixtureTransport]:
    ft = FixtureTransport(RUNWAY_DIR)
    kwargs = {"now": now} if now is not None else {}
    return RunwayVideo(with_runway(manifests, engine), transport=ft, **kwargs), ft


def still(tmp_path: Path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def frame(tmp_path: Path, name: str, data: bytes, w: int = 1920, h: int = 1080) -> dict:
    return {"path": still(tmp_path, name, data), "w": w, "h": h}


def calls(ft: FixtureTransport, method: str, suffix: str) -> list:
    return [c for c in ft.served if c.method == method and c.path.endswith(suffix)]


def sent(ft: FixtureTransport, suffix: str) -> dict:
    found = calls(ft, "POST", suffix)
    assert len(found) == 1, [(c.method, c.path) for c in ft.served]
    return json.loads(found[0].body)


def o3_inputs(tmp_path: Path, w: int = 1920, h: int = 1080) -> dict:
    return {
        "image": frame(tmp_path, "start_a.png", STILL_A, w, h),
        "image_tail": {"path": still(tmp_path, "end_a.png", STILL_END)},
        "prompt": O3_PROMPT,
    }


def v3_inputs(tmp_path: Path, prompt: str = V3_PROMPT) -> dict:
    return {"image": frame(tmp_path, "start_a.png", STILL_A), "prompt": prompt}


def submit_o3(manifests, tmp_path, **kw):
    ad, ft = make(manifests, "kling-o3-4k", **kw)
    job = ad.submit(
        Staged(o3_inputs(tmp_path), {"duration": 5, "negative_prompt": O3_NEGATIVE, "resolution": "4K"})
    )
    return ad, ft, job


# ---- construction ------------------------------------------------------------------------------------------


def test_the_adapter_declares_the_money_rules():
    assert RunwayVideo.PROVIDER == "runway"
    assert set(RunwayVideo.ENGINE_IDS) == set(SLUGS)
    assert RunwayVideo.idempotent_submit is False and RunwayVideo.supports_lookup is False
    assert (RunwayVideo.OUTPUT_FPS, RunwayVideo.POLL_INTERVAL_S, RunwayVideo.MIN_POLL_GAP_S) == (
        24.0,
        6.0,
        5.0,
    )


@pytest.mark.parametrize("engine", sorted(SLUGS))
def test_no_transport_means_no_adapter(manifests, engine):
    with pytest.raises(MissingTransport):
        RunwayVideo(with_runway(manifests, engine))


@pytest.mark.parametrize("engine", sorted(SLUGS))
def test_the_model_comes_from_the_route_slug(manifests, engine):
    ad, _ = make(manifests, engine)
    assert ad.model_id == SLUGS[engine] == ad.model_version
    assert ad.route_id == "runway" and ad.custody_grade == "D"


def test_a_runway_route_without_a_slug_is_refused(manifests):
    m = manifests["kling-o3-4k"]
    bare = runway_route("kling-o3-4k").model_copy(update={"model_slug": None})
    m = m.model_copy(update={"routes": [r for r in m.routes if r.provider != "runway"] + [bare]})
    with pytest.raises(ValueError, match="model_slug"):
        RunwayVideo(m, transport=FixtureTransport(RUNWAY_DIR))


def test_it_drives_only_its_engines_and_only_the_runway_route(manifests):
    ft = FixtureTransport(RUNWAY_DIR)
    with pytest.raises(ValueError, match="speaks to 'runway'"):
        RunwayVideo(with_runway(manifests, "kling-o3-4k"), "atlas", ft)
    with pytest.raises(ValueError, match="drives"):
        RunwayVideo(manifests["seedream-5-pro"], None, ft)
    with pytest.raises(ValueError, match="no route 'runway'"):
        RunwayVideo(
            manifests["kling-o3-4k"].model_copy(update={"routes": manifests["kling-o3-4k"].routes[:1]})
        )


# ---- bodies ------------------------------------------------------------------------------------------------


def test_kling_o3_start_end_body_headers_and_folded_negative(manifests, tmp_path):
    ad, ft, job = submit_o3(manifests, tmp_path)
    assert job == TASK
    body = sent(ft, "/v1/image_to_video")
    assert body == body_kling_o3()
    assert "negativePrompt" not in body and "resolution" not in body  # folded; 4K is the slug
    with pytest.raises(ValueError, match="resolution"):
        runway_schema.check_body("klingO3_4k", "image_to_video", {**body, "resolution": "4K"})
    headers = calls(ft, "POST", "/v1/image_to_video")[0].headers
    assert headers["x-runway-version"] == "2024-11-06"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert headers["content-type"] == "application/json"
    assert ad.notes(job) == []  # 5 s is legal on Kling: no snap


def test_kling_o3_poll_runs_throttled_pending_running_succeeded(manifests, tmp_path):
    clock = Clock()
    ad, ft, job = submit_o3(manifests, tmp_path, now=clock)
    seen = []
    for _ in range(4):
        clock.sleep(6.0)
        seen.append(ad.poll(job))
    assert [s.state for s in seen] == ["queued", "queued", "running", "done"]
    assert seen[0].message.startswith("throttled: at the account's concurrency limit")
    assert seen[1].message == "" and seen[2].message == "progress 0.40"
    done = seen[3]
    assert done.outputs == [OUTPUT_URL]
    assert done.output_fps == 24.0 and done.output_bits is None
    assert done.usage == {"credits": 215, "usd": 2.15, "estimated_credits": 215}
    assert ad.fetch(done.outputs[0]) == RESULT and len(RESULT) == 48
    download = calls(ft, "GET", "/output.mp4")
    assert len(download) == 1 and "authorization" not in download[0].headers  # signed URL, no credentials


def test_a_second_look_inside_5_seconds_makes_no_request(manifests, tmp_path):
    clock = Clock()
    ad, ft, job = submit_o3(manifests, tmp_path, now=clock)
    first_look = ad.poll(job)
    clock.sleep(3.0)
    again = ad.poll(job)
    assert again == first_look
    assert len(calls(ft, "GET", f"/v1/tasks/{TASK}")) == 1
    clock.sleep(2.5)
    assert ad.poll(job).state == "queued"  # PENDING, a new look
    assert len(calls(ft, "GET", f"/v1/tasks/{TASK}")) == 2


def test_wait_on_the_poll_fixtures_returns_done(manifests, tmp_path):
    clock = Clock()
    ad, _, job = submit_o3(manifests, tmp_path, now=clock)
    status = ad.wait(job, sleep=clock.sleep, now=clock)
    assert status.state == "done" and status.outputs == [OUTPUT_URL]


def test_kling_v3_body_carries_cfg_scale_and_the_1080p_ratio(manifests, tmp_path):
    ad, ft = make(manifests, "kling-v3-pro")
    assert ad.submit(Staged(v3_inputs(tmp_path), {})) == TASK
    assert sent(ft, "/v1/image_to_video") == body_kling_v3()


def test_veo_snaps_5_to_6_sends_negative_prompt_and_seed(manifests, tmp_path):
    ad, ft = make(manifests, "veo-3.1")
    inputs = {
        "start_image": frame(tmp_path, "start_a.png", STILL_A),
        "end_image": {"path": still(tmp_path, "end_a.png", STILL_END)},
        "prompt": VEO_PROMPT,
    }
    job = ad.submit(Staged(inputs, {"duration": 5, "negative_prompt": VEO_NEGATIVE, "seed": 1234}))
    assert job == VEO_TASK
    assert sent(ft, "/v1/image_to_video") == body_veo()
    assert ad.notes(job) == ["duration 5 -> 6 (veo3.1 accepts 4, 6, 8)"]
    clock_free = [ad.poll(job), ad.poll(job)]  # the real clock: the second look is the cached first
    assert clock_free[0] == clock_free[1] and clock_free[0].state == "running"


def test_veo_poll_reaches_done(manifests, tmp_path):
    clock = Clock()
    ad, ft = make(manifests, "veo-3.1", now=clock)
    states = []
    for _ in range(2):
        clock.sleep(6.0)
        states.append(ad.poll(VEO_TASK))
    assert [s.state for s in states] == ["running", "done"]
    assert states[1].outputs == [OUTPUT_URL] and states[1].usage["credits"] == 120


def test_seedance_i2v_1080p_body_and_10_bit_output(manifests, tmp_path):
    clock = Clock()
    ad, ft = make(manifests, "seedance-2.5-i2v", now=clock)
    inputs = {"image": frame(tmp_path, "start_a.png", STILL_A), "prompt": SEEDANCE_PROMPT}
    job = ad.submit(Staged(inputs, {"resolution": "1080p", "seed": 7}))
    body = sent(ft, "/v1/image_to_video")
    assert body == body_seedance_i2v() and "draft" not in body
    status = ad.wait(job, sleep=clock.sleep, now=clock)
    assert (status.state, status.output_fps, status.output_bits) == ("done", 24.0, 10)


def test_seedance_480p_is_a_runway_draft(manifests, tmp_path):
    ad, ft = make(manifests, "seedance-2.5-i2v")
    inputs = {"image": frame(tmp_path, "start_a.png", STILL_A), "prompt": SEEDANCE_PROMPT}
    ad.submit(Staged(inputs, {"duration": 4}))  # 480P is the manifest default
    assert sent(ft, "/v1/image_to_video") == body_seedance_draft()


def test_seedance_r2v_is_text_to_video_with_references_and_the_uploaded_clay(manifests, tmp_path):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    clay = tmp_path / "clay.mp4"
    clay.write_bytes(CLAY)
    inputs = {
        "reference_images": [
            {"path": still(tmp_path, "hero.png", STILL_A), "role": "first_frame", "w": 1920, "h": 1080},
            {"path": still(tmp_path, "room.png", STILL_B), "role": "reference_image"},
        ],
        "reference_videos": [str(clay)],
        "prompt": R2V_PROMPT,
    }
    job = ad.submit(Staged(inputs, {"duration": 8, "resolution": "480P", "ratio": "16:9"}))
    assert job == TASK
    assert [(c.method, c.host, c.path) for c in ft.served] == [
        ("POST", "api.dev.runwayml.com", "/v1/uploads"),
        ("POST", "plate-test-uploads.s3.amazonaws.com", "/"),
        ("POST", "api.dev.runwayml.com", "/v1/text_to_video"),
    ]
    assert sent(ft, "/v1/uploads") == {"filename": opaque_name(CLAY, "mp4"), "type": "ephemeral"}
    hop = ft.served[1]
    assert "authorization" not in hop.headers and "x-runway-version" not in hop.headers
    assert hop.headers["content-type"].startswith("multipart/form-data")
    assert sent(ft, "/v1/text_to_video") == body_seedance_r2v()
    assert "reference roles are not a Runway field: first_frame sent as a plain reference" in ad.notes(job)


def test_seedance_r2v_refuses_text_to_video_by_another_name(manifests):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    with pytest.raises(InputError, match="text-to-video"):
        ad.submit(Staged({"reference_images": [], "prompt": R2V_PROMPT}, {}))
    assert ft.served == []


def test_gen45_takes_a_start_frame_and_follows_the_frame(manifests, tmp_path):
    ad, ft = make(manifests, "gen-4.5")
    job = ad.submit(Staged({"image": frame(tmp_path, "start_a.png", STILL_A), "prompt": GEN_PROMPT}, {}))
    assert job == TASK
    body = sent(ft, "/v1/image_to_video")
    assert body == body_gen45() and "audio" not in body  # Gen-4.5 has no audio field


def test_kling_o3_elements_become_references_beside_two_keyframes(manifests, tmp_path):
    ad, ft = make(manifests, "kling-o3-4k")
    el = still(tmp_path, "element.png", STILL_B)
    inputs = {
        **o3_inputs(tmp_path),
        "prompt": ELEMENTS_PROMPT,
        "elements": [{"name": n, "path": el} for n in ("sofa", "lamp", "rug")],
    }
    ad.submit(Staged(inputs, {}))
    body = sent(ft, "/v1/image_to_video")
    assert body == body_elements()
    assert len(body["promptImage"]) + len(body["references"]) == 5


def test_more_than_seven_images_on_kling_o3_is_refused(manifests, tmp_path):
    ad, ft = make(manifests, "kling-o3-4k")
    el = still(tmp_path, "element.png", STILL_B)
    inputs = {**o3_inputs(tmp_path), "elements": [{"name": f"e{i}", "path": el} for i in range(6)]}
    with pytest.raises(InputError, match="at most 7 images"):
        ad.submit(Staged(inputs, {}))
    assert ft.served == []


def test_a_4mb_start_frame_goes_through_uploads(manifests, tmp_path):
    ad, ft = make(manifests, "kling-v3-pro")
    big = big_png()
    assert not runway_schema.data_uri_fits(len(big), "image/png")
    inputs = {"image": frame(tmp_path, "big.png", big), "prompt": UPLOAD_PROMPT}
    assert ad.submit(Staged(inputs, {})) == TASK
    assert sent(ft, "/v1/uploads") == {"filename": opaque_name(big, "png"), "type": "ephemeral"}
    body = sent(ft, "/v1/image_to_video")
    assert body == body_kling_v3(UPLOAD_PROMPT, BIG_URI)
    assert body["promptImage"][0]["uri"].startswith("runway://")


def test_draft_enhance_sends_exactly_two_keys(manifests):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    assert ad.submit(Staged({"draft_task_id": DRAFT_TASK}, {})) == ENHANCED_TASK
    assert sent(ft, "/v1/text_to_video") == {"model": "seedance2_5", "draftTaskId": DRAFT_TASK}


def test_draft_enhance_refuses_anything_but_the_draft_id(manifests, tmp_path):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    with pytest.raises(InputError, match="no params"):
        ad.submit(Staged({"draft_task_id": DRAFT_TASK}, {"duration": 8}))
    with pytest.raises(InputError, match="only the draft task id"):
        ad.submit(Staged({"draft_task_id": DRAFT_TASK, "prompt": R2V_PROMPT}, {}))
    with pytest.raises(InputError, match="draft"):
        ad.quote({"draft_task_id": DRAFT_TASK}, {})
    assert ft.served == []


# ---- refuse or fold, never drop ------------------------------------------------------------------------------


def test_an_off_ratio_frame_on_kling_o3_is_refused_naming_the_legal_ratios(manifests, tmp_path):
    ad, ft = make(manifests, "kling-o3-4k")
    with pytest.raises(InputError) as exc:
        ad.submit(Staged(o3_inputs(tmp_path, 1920, 804), {}))
    text = str(exc.value)
    assert "no legal ratio for 1920x804 on klingO3_4k" in text
    assert "3840:2160, 2160:3840, 2880:2880" in text and "atlas route" in text
    assert ft.served == []  # refused before any byte left


def test_no_frame_size_means_no_ratio_at_submit_and_auto_in_the_quote(manifests):
    ad, ft = make(manifests, "kling-v3-pro")
    inputs = {"image": "https://example.com/start.png", "prompt": V3_PROMPT}
    with pytest.raises(InputError, match="source dimensions"):
        ad.submit(Staged(inputs, {}))
    q = ad.quote(inputs, {})
    assert (q.size.w, q.size.h, q.size.label) == (0, 0, "auto")
    assert ft.served == []


@pytest.mark.parametrize("engine", ["kling-o3-4k", "kling-v3-pro"])
def test_a_seed_on_kling_is_a_param_error(manifests, tmp_path, engine):
    ad, ft = make(manifests, engine)
    with pytest.raises(ParamError, match="seed"):
        ad.submit(Staged(v3_inputs(tmp_path), {"seed": 3}))
    with pytest.raises(ParamError, match="pin the atlas route"):
        ad.quote(v3_inputs(tmp_path), {"seed": 3})
    assert ft.served == []


def test_cfg_scale_other_than_the_default_on_kling_o3_is_a_param_error(manifests, tmp_path):
    ad, _ = make(manifests, "kling-o3-4k")
    with pytest.raises(ParamError, match="cfg_scale 0.7"):
        ad.submit(Staged(o3_inputs(tmp_path), {"cfg_scale": 0.7}))


def test_elements_on_kling_v3_are_an_input_error(manifests, tmp_path):
    ad, _ = make(manifests, "kling-v3-pro")
    inputs = {
        **v3_inputs(tmp_path),
        "elements": [{"name": "sofa", "path": still(tmp_path, "e.png", STILL_B)}],
    }
    with pytest.raises(InputError, match="elements"):
        ad.submit(Staged(inputs, {}))


@pytest.mark.parametrize("flag", ["camera_fixed", "return_last_frame"])
def test_seedance_flags_runway_cannot_express_are_param_errors(manifests, tmp_path, flag):
    ad, _ = make(manifests, "seedance-2.5-i2v")
    inputs = {"image": frame(tmp_path, "start_a.png", STILL_A), "prompt": SEEDANCE_PROMPT}
    with pytest.raises(ParamError, match=flag):
        ad.submit(Staged(inputs, {flag: True}))


def test_kling_resolution_must_be_the_slugs_tier(manifests, tmp_path):
    ad, _ = make(manifests, "kling-o3-4k")
    with pytest.raises(ParamError, match="tier 4K"):
        ad.submit(Staged(o3_inputs(tmp_path), {"resolution": "1080P"}))
    assert ad.quote(o3_inputs(tmp_path), {"resolution": "4k"}).size.label == "4K"


def test_a_prompt_over_the_model_cap_is_refused_not_truncated(manifests, tmp_path):
    ad, _ = make(manifests, "kling-v3-pro")
    with pytest.raises(InputError, match="2501 characters"):
        ad.submit(Staged(v3_inputs(tmp_path, "x" * 2501), {}))


# ---- create answers ----------------------------------------------------------------------------------------


def test_a_safety_400_is_a_stateless_refusal(manifests, tmp_path):
    ad, ft = make(manifests, "kling-v3-pro")
    job = ad.submit(Staged(v3_inputs(tmp_path, PROBES["safety"]), {}))
    assert refusal_from_job_id(job) == "runway:SAFETY.INPUT.IMAGE: Input image rejected"
    fresh, _ = make(manifests, "kling-v3-pro")  # nothing lives on the adapter
    status = fresh.poll(job)
    assert status.state == "refused" and "SAFETY.INPUT.IMAGE" in status.message
    fresh.cancel(job)  # nothing to cancel at the vendor: no request
    assert len(calls(ft, "POST", "/v1/image_to_video")) == 1


def test_a_400_with_issues_names_the_path(manifests, tmp_path):
    ad, _ = make(manifests, "kling-v3-pro")
    with pytest.raises(VideoSubmitError) as exc:
        ad.submit(Staged(v3_inputs(tmp_path, PROBES["issues"]), {}))
    assert str(exc.value) == "runway 400: Validation of body failed at promptImage"


@pytest.mark.parametrize(
    ("probe", "text"),
    [
        ("429", "runway 429: daily generation cap or rate limit; not retried"),
        ("503", "runway 503: not retried (no idempotency key"),
        ("401", "runway 401: the key was refused (RUNWAYML_API_SECRET)"),
    ],
)
def test_429_503_and_401_raise_after_exactly_one_create(manifests, tmp_path, probe, text):
    ad, ft = make(manifests, "kling-v3-pro")
    with pytest.raises(VideoSubmitError) as exc:
        ad.submit(Staged(v3_inputs(tmp_path, PROBES[probe]), {}))
    assert str(exc.value).startswith(text)
    assert KEY not in str(exc.value)
    assert len(calls(ft, "POST", "/v1/image_to_video")) == 1


def test_a_create_timeout_propagates_and_is_never_retried(manifests, tmp_path):
    attempts = []

    def boom(request):
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("slow vendor", request=request)

    ad = RunwayVideo(with_runway(manifests, "kling-v3-pro"), transport=httpx.MockTransport(boom))
    with pytest.raises(httpx.ReadTimeout):
        ad.submit(Staged(v3_inputs(tmp_path), {}))
    assert attempts == ["/v1/image_to_video"]


def test_no_key_fails_before_anything_leaves(manifests, tmp_path, monkeypatch):
    monkeypatch.delenv("RUNWAYML_API_SECRET")
    ad, ft = make(manifests, "seedance-2.5-r2v")
    clay = tmp_path / "clay.mp4"
    clay.write_bytes(CLAY)
    inputs = {
        "reference_images": [{"path": still(tmp_path, "hero.png", STILL_A)}],
        "reference_videos": [str(clay)],
        "prompt": R2V_PROMPT,
    }
    with pytest.raises(VideoSubmitError, match="no Runway key: set RUNWAYML_API_SECRET"):
        ad.submit(Staged(inputs, {}))
    assert ft.served == []  # not even the upload


def test_the_key_appears_in_the_header_only(manifests, tmp_path):
    messages = []
    ad, ft = make(manifests, "kling-v3-pro")
    for probe in ("issues", "429", "503", "401"):
        with pytest.raises(VideoSubmitError) as exc:
            ad.submit(Staged(v3_inputs(tmp_path, PROBES[probe]), {}))
        messages.append(str(exc.value))
    messages.append(refusal_from_job_id(ad.submit(Staged(v3_inputs(tmp_path, PROBES["safety"]), {}))) or "")
    for job in (SAFETY_TASK, BAD_OUTPUT_TASK, THIRD_PARTY_TASK, ASSET_TASK, CANCELLED_TASK, MISSING_TASK):
        messages.append(ad.poll(job).message)
    assert messages and not any(KEY in m for m in messages)
    assert not any(KEY.encode() in c.body for c in ft.served)
    auth = [c.headers.get("authorization") for c in ft.served]
    assert all(a == f"Bearer {KEY}" for a in auth)  # every call here is api.dev.runwayml.com


# ---- poll answers ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "state", "prefix", "suffix"),
    [
        (
            SAFETY_TASK,
            "refused",
            "refused: runway:SAFETY.INPUT.TEXT: ",
            "(credits are not refunded for SAFETY.INPUT.*)",
        ),
        (BAD_OUTPUT_TASK, "failed", "bad_output: runway:INTERNAL.BAD_OUTPUT.CODE01: ", "then re-quote)"),
        (THIRD_PARTY_TASK, "failed", "transient: runway:THIRD_PARTY.UNAVAILABLE: ", "(wait, then re-quote)"),
        (ASSET_TASK, "failed", "input: runway:ASSET.INVALID: ", "dimensions."),
    ],
)
def test_failure_codes_map_by_prefix(manifests, job, state, prefix, suffix):
    ad, _ = make(manifests, "kling-v3-pro")
    status = ad.poll(job)
    assert status.state == state
    assert status.message.startswith(prefix) and status.message.endswith(suffix)
    assert "credits" in status.usage


def test_cancelled_and_missing_tasks_are_failed(manifests):
    ad, _ = make(manifests, "kling-v3-pro")
    cancelled = ad.poll(CANCELLED_TASK)
    assert (cancelled.state, cancelled.message) == ("failed", "cancelled at the vendor")
    missing = ad.poll(MISSING_TASK)
    assert (missing.state, missing.message) == ("failed", "task not found (deleted or cancelled)")


def test_a_503_on_the_poll_reads_as_running_then_the_task_finishes(manifests):
    clock = Clock()
    ad, _ = make(manifests, "kling-v3-pro", now=clock)
    busy = ad.poll(BUSY_TASK)
    assert (busy.state, busy.message) == ("running", "transient 503")
    clock.sleep(6.0)
    assert ad.poll(BUSY_TASK).state == "done"


def test_cancel_is_a_delete_and_a_404_is_ignored(manifests):
    ad, ft = make(manifests, "kling-v3-pro")
    ad.cancel(TASK)
    ad.cancel(GONE_TASK)
    deletes = [c for c in ft.served if c.method == "DELETE"]
    assert [c.path for c in deletes] == [f"/v1/tasks/{TASK}", f"/v1/tasks/{GONE_TASK}"]
    assert all(c.headers["authorization"] == f"Bearer {KEY}" for c in deletes)


# ---- hosts and downloads -------------------------------------------------------------------------------------


def test_fetch_is_https_and_allowlisted_only(manifests):
    ad, ft = make(manifests, "kling-v3-pro")
    with pytest.raises(FetchError, match="https"):
        ad.fetch("http://dnznrvs05pmza.cloudfront.net/output.mp4")
    with pytest.raises(HostNotAllowed, match="example.com"):
        ad.fetch("https://outputs.example.com/output.mp4")
    assert ft.served == []


def test_an_upload_host_off_the_list_fails_closed_naming_the_host(manifests, tmp_path):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    clay = tmp_path / "clay.mp4"
    clay.write_bytes(CLAY_OFFLIST)
    inputs = {"reference_images": [frame(tmp_path, "hero.png", STILL_A)], "reference_videos": [str(clay)]}
    with pytest.raises(HostNotAllowed, match="uploads.example.net"):
        ad.submit(Staged({**inputs, "prompt": R2V_PROMPT}, {}))
    assert [c.path for c in ft.served] == ["/v1/uploads"]  # no multipart hop, no create


@pytest.mark.parametrize(
    ("clip", "text", "paths"),
    [
        (CLAY_INIT_429, "runway upload init failed: 429 Too many uploads", ["/v1/uploads"]),
        (CLAY_PUT_403, "runway upload failed: 403; not retried (start over)", ["/v1/uploads", "/"]),
    ],
)
def test_a_failed_upload_is_not_retried_and_no_task_is_created(manifests, tmp_path, clip, text, paths):
    ad, ft = make(manifests, "seedance-2.5-r2v")
    clay = tmp_path / "clay.mp4"
    clay.write_bytes(clip)
    inputs = {"reference_images": [frame(tmp_path, "hero.png", STILL_A)], "reference_videos": [str(clay)]}
    with pytest.raises(VideoSubmitError) as exc:
        ad.submit(Staged({**inputs, "prompt": R2V_PROMPT}, {}))
    assert str(exc.value) == text
    assert [c.path for c in ft.served] == paths


def test_the_recordings_cover_only_runway_hosts():
    ft = FixtureTransport(RUNWAY_DIR)
    hosts = {r.host for r in ft.recordings}
    assert hosts == {
        "api.dev.runwayml.com",
        "dnznrvs05pmza.cloudfront.net",
        "plate-test-uploads.s3.amazonaws.com",
    }
    with pytest.raises(UnrecordedRequest):
        ft.handle_request(httpx.Request("GET", f"{API}/tasks/not-a-recorded-task"))


# ---- quotes ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "params", "usd"),
    [
        ("kling-o3-4k", {"duration": 5}, 2.15),
        ("kling-v3-pro", {"duration": 5}, 0.60),
        ("veo-3.1", {"duration": 5}, 1.20),
        ("veo-3.1", {"duration": 5, "generate_audio": True}, 2.40),
        ("seedance-2.5-i2v", {"duration": 15, "resolution": "1080P"}, 10.20),
        ("seedance-2.5-i2v", {"duration": 4, "resolution": "480P"}, 0.80),
        ("gen-4.5", {"duration": 5}, 0.60),
    ],
)
def test_quotes_follow_the_runway_catalogue(manifests, tmp_path, engine, params, usd):
    ad, ft = make(manifests, engine)
    name = "start_image" if engine == "veo-3.1" else "image"
    q = ad.quote({name: frame(tmp_path, "start_a.png", STILL_A), "prompt": "Camera holds."}, params)
    assert q.price_usd == pytest.approx(usd)
    assert (q.route_id, q.custody_grade, q.price_status) == ("runway", "D", "confirmed")
    assert q.model_version == SLUGS[engine]
    assert ft.served == []  # a quote makes no request


def test_the_quote_sizes_the_frame_from_the_ratio_and_its_tier(manifests, tmp_path):
    ad, _ = make(manifests, "kling-o3-4k")
    q = ad.quote(o3_inputs(tmp_path), {"duration": 5})
    assert q.size == EngineSize(w=3840, h=2160, label="4K")
    assert q.eta_s == pytest.approx(90 + 8 * 5)
    seedance, _ = make(manifests, "seedance-2.5-i2v")
    q = seedance.quote({"image": frame(tmp_path, "s.png", STILL_A), "prompt": "p"}, {"duration": 4})
    assert q.size == EngineSize(w=854, h=480, label="480P")
    veo, _ = make(manifests, "veo-3.1")
    q = veo.quote({"start_image": frame(tmp_path, "v.png", STILL_A), "prompt": "p"}, {"duration": 5})
    assert q.size == EngineSize(w=1920, h=1080, label="1080P") and q.eta_s == pytest.approx(90 + 8 * 6)


def test_durations_snap_up_to_what_each_runway_model_takes(manifests):
    kling, _ = make(manifests, "kling-v3-pro")
    assert [kling.clamp_duration(x) for x in (0.4, 2.0, 5.2, 15.9)] == [3, 3, 6, 15]
    veo, _ = make(manifests, "veo-3.1")
    assert [veo.clamp_duration(x) for x in (0, 5, 7, 12)] == [4, 6, 8, 8]


def test_resolution_spellings_map_onto_the_manifests_own(manifests):
    veo, _ = make(manifests, "veo-3.1")
    assert veo.validate_params({"resolution": "720P"})["resolution"] == "720p"
    seedance, _ = make(manifests, "seedance-2.5-i2v")
    assert seedance.validate_params({"resolution": 1080})["resolution"] == "1080P"
    with pytest.raises(ParamError):
        seedance.validate_params({"resolution": "4K"})


# ---- Wave 12 (T138): the params agree with the route --------------------------------------------------------

NEGATIVE = "morphing, warping, new objects"
FOLDING = ("seedance-2.5-i2v", "seedance-2.5-r2v", "gen-4.5")  # manifests that declare no negative_prompt


@pytest.mark.parametrize("engine", FOLDING)
def test_validate_params_takes_a_negative_prompt_the_manifest_does_not_declare(manifests, engine):
    ad, _ = make(manifests, engine)
    assert "negative_prompt" not in {p.name for p in ad.manifest.params}
    assert ad.validate_params({"negative_prompt": f"  {NEGATIVE} "})["negative_prompt"] == NEGATIVE
    assert "negative_prompt" not in ad.validate_params({"negative_prompt": "  "})
    assert "negative_prompt" not in ad.validate_params({"negative_prompt": None})
    with pytest.raises(ParamError, match="expected a string"):
        ad.validate_params({"negative_prompt": 3})
    with pytest.raises(
        ParamError, match="unknown parameter"
    ):  # every other name stays the manifest's to check
        ad.validate_params({"negative_prompt": NEGATIVE, "negativePrompt": NEGATIVE})


def body_inputs(tmp_path: Path, engine: str) -> dict:
    if engine == "seedance-2.5-r2v":
        return {"reference_images": [frame(tmp_path, "hero.png", STILL_A)], "prompt": R2V_PROMPT}
    name = "start_image" if engine == "veo-3.1" else "image"
    return {name: frame(tmp_path, "start_a.png", STILL_A), "prompt": SEEDANCE_PROMPT}


@pytest.mark.parametrize("engine", sorted(SLUGS))
def test_the_body_never_carries_a_negative_prompt_key(manifests, tmp_path, engine):
    ad, ft = make(manifests, engine)
    parsed = ad.parse_inputs(body_inputs(tmp_path, engine))
    endpoint, body = ad.build_body(parsed, ad.validate_params({"negative_prompt": NEGATIVE}), (1920, 1080))
    assert "negative_prompt" not in body
    if engine == "veo-3.1":  # the one model with the field sends it as the field
        assert body["negativePrompt"] == NEGATIVE and "Avoid:" not in body["promptText"]
    else:  # every other model folds it as the final line
        assert "negativePrompt" not in body and body["promptText"].endswith(f"\nAvoid: {NEGATIVE}")
    runway_schema.check_body(SLUGS[engine], endpoint, body)
    assert ft.served == []  # small frames go inline: building the body sends nothing


def test_check_expressible_raises_what_submit_raises_without_reading_a_file(manifests):
    start = {"image": "a" * 64, "prompt": O3_PROMPT}  # hashes, not files: only presence is read
    pair = {**start, "image_tail": "b" * 64}
    o3, ft = make(manifests, "kling-o3-4k")
    o3.check_expressible(o3.validate_params({"negative_prompt": NEGATIVE}), pair)
    o3.check_expressible(o3.validate_params({}), {**pair, "elements": ["e"] * 5})
    with pytest.raises(ParamError, match="seed is not expressible on the runway route; pin the atlas route"):
        o3.check_expressible(o3.validate_params({"seed": -1}), start)
    with pytest.raises(ParamError, match="cfg_scale 0.7"):
        o3.check_expressible(o3.validate_params({"cfg_scale": 0.7}), start)
    with pytest.raises(InputError, match="at most 5 elements"):
        o3.check_expressible(o3.validate_params({}), {**pair, "elements": ["e"] * 6})
    v3, _ = make(manifests, "kling-v3-pro")
    with pytest.raises(InputError, match="elements"):
        v3.check_expressible(v3.validate_params({}), {**start, "elements": ["e"]})
    seedance, _ = make(manifests, "seedance-2.5-i2v")
    seedance.check_expressible(seedance.validate_params({"negative_prompt": NEGATIVE}), start)
    with pytest.raises(ParamError, match="camera_fixed true"):
        seedance.check_expressible(seedance.validate_params({"camera_fixed": True}), start)
    gen, _ = make(manifests, "gen-4.5")
    with pytest.raises(InputError, match="start frame only"):
        gen.check_expressible(gen.validate_params({}), pair)
    cap = runway_schema.spec_for("gen4.5").prompt_max
    with pytest.raises(InputError, match="the limit is"):  # the folded line counts against the prompt cap
        gen.check_expressible(gen.validate_params({"negative_prompt": "x" * cap}), start)
    assert ft.served == []


def succeeded_transport(looks: list[list[str]]) -> httpx.MockTransport:
    """GET /v1/tasks/{id}: SUCCEEDED, with looks[n] as the output list of look n (the last one repeated)."""
    seen: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", f"/v1/tasks/{TASK}")
        out = looks[min(len(seen), len(looks) - 1)]
        seen.append(1)
        return httpx.Response(200, json={"id": TASK, "status": "SUCCEEDED", "output": out})

    return httpx.MockTransport(answer)


@pytest.mark.parametrize(
    ("looks", "states"),
    [
        ([[], [], [], [OUTPUT_URL]], ["running", "running", "running", "done"]),
        ([[], [], [], []], ["running", "running", "running", "failed"]),
    ],
)
def test_succeeded_without_an_output_is_transient_for_three_looks(manifests, looks, states):
    clock = Clock()
    ad = RunwayVideo(with_runway(manifests, "kling-v3-pro"), transport=succeeded_transport(looks), now=clock)
    seen = []
    for _ in looks:
        clock.sleep(6.0)
        seen.append(ad.poll(TASK))
    assert [s.state for s in seen] == states
    assert all(
        s.message == "transient: runway SUCCEEDED without an output" for s in seen if s.state != "done"
    )
    if states[-1] == "done":
        assert seen[-1].outputs == [OUTPUT_URL] and seen[-1].output_fps == 24.0
    assert RunwayVideo.EMPTY_SUCCESS_LOOKS == 3
