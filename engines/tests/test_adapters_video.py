"""Contract tests for the video engine adapters (router plan T18).

Atlas Seedance 2.5 i2v/r2v, Kling O3 4K and v3 Pro, Vertex Veo 3.1, the local Wan 2.2 VACE stub, and the BytePlus and
WaveSpeed same-model fallbacks. Every cloud adapter runs against recorded fixtures (contract/atlas_video, vertex,
byteplus, wavespeed; synthetic, authored from the documented request and response shapes); a request the recordings do
not cover raises UnrecordedRequest, so each test also pins the exact body the adapter sends. conftest.py blocks sockets.

Doctrine checked here: no text-to-video, no invented camera (ratio follows the source, adaptive for edits), Wan clips
are PREVIS always, and a fallback only ever serves the same model after a new quote.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import struct
import sys
import zlib
from pathlib import Path

import pytest

from plate_engines.adapters import local_wan, seedance_fallbacks
from plate_engines.adapters.atlas_video import (
    SeedanceI2V,
    SeedanceR2V,
    VideoTimeout,
    atlas_video_adapter,
    check_output_depends_on_input,
    frame_size,
    inputs_digest,
    parse_ref,
    refusal_from_job_id,
    refused_job_id,
    sniff_image_size,
    timing_hints,
)
from plate_engines.adapters.base import (
    FetchError,
    InputError,
    MissingTransport,
    ParamError,
    SilentlyIgnoredInput,
    Staged,
)
from plate_engines.adapters.local_wan import LocalModelUnavailable, WanVace
from plate_engines.adapters.seedance_fallbacks import (
    BytePlusSeedance,
    Fallback,
    FallbackError,
    RequoteRequired,
    WaveSpeedSeedance,
    requote_needed,
)
from plate_engines.adapters.transport import (
    CONTRACT_DIR,
    FixtureTransport,
    HostNotAllowed,
    UnrecordedRequest,
    write_recording,
)
from plate_engines.adapters.vertex_veo import VertexVeo
from plate_engines.atlas import AtlasError
from plate_engines.models import EngineSize

ATLAS_KEY = "atlas-test-key"
ARK_KEY = "ark-test-key"
WAVESPEED_KEY = "wavespeed-test-key"
VERTEX_TOKEN = "ya29.test-token"
PROJECT = "plate-test"

MEDIA = "https://static.atlascloud.ai/media/plate/"
OSS = "https://atlas-media.oss-ap-southeast-1.aliyuncs.com/vid/"
TOS = "https://ark-content-generation-ap-southeast-1.tos-ap-southeast-1.bytepluses.com/seedance/"
WS_CDN = "https://cdn.wavespeed.ai/outputs/"
PROMPT = "Camera holds still; late sun moves across the oak floor."
VEO_PROMPT = "Camera holds; steam rises from the cup."


# ---- deterministic fixtures: the stills and clips the recordings were authored against ---------------------


def png_bytes(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """A valid solid-colour RGB PNG written with a stored (uncompressed) deflate block, so the bytes are identical on every
    OS and zlib build; the recordings embed their base64 and match on the body hash."""
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


STILL_A = png_bytes(16, 9, (200, 40, 40))
STILL_B = png_bytes(16, 9, (40, 40, 200))
STILL_PORTRAIT = png_bytes(9, 16, (40, 200, 40))
STILL_END = png_bytes(16, 9, (240, 240, 240))
CLIP_A = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"PLATE-FIXTURE-CLIP-A" * 4
CLIP_B = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"PLATE-FIXTURE-CLIP-B" * 4


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + b64(data)


def still_file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---- harness ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ATLASCLOUD_KEY", ATLAS_KEY)
    monkeypatch.setenv("ARK_API_KEY", ARK_KEY)
    monkeypatch.setenv("WAVESPEED_API_KEY", WAVESPEED_KEY)
    monkeypatch.setenv("VERTEX_ACCESS_TOKEN", VERTEX_TOKEN)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.delenv("PLATE_LIVE", raising=False)
    monkeypatch.delenv("PLATE_STUB_LOCAL", raising=False)


def fixtures(folder: str) -> FixtureTransport:
    return FixtureTransport(CONTRACT_DIR / folder)


def atlas(manifests, engine, route=None):
    ft = fixtures("atlas_video")
    return atlas_video_adapter(manifests[engine], route, ft), ft


def sent(ft: FixtureTransport, suffix: str) -> dict:
    """The JSON body of the (single) served call whose path ends with suffix."""
    calls = [c for c in ft.served if c.path.endswith(suffix)]
    assert len(calls) == 1, [c.path for c in ft.served]
    return json.loads(calls[0].body)


def header(ft: FixtureTransport, suffix: str, name: str) -> str:
    return next(c for c in ft.served if c.path.endswith(suffix)).headers[name]


def finish(adapter, job, tries: int = 6):
    """poll() until the job leaves queued and running (the recordings serve processing first, then the result)."""
    status = None
    for _ in range(tries):
        status = adapter.poll(job)
        if status.state in ("done", "failed", "refused"):
            return status
    raise AssertionError(f"{job} never finished: {status}")


def staged(inputs, params=None, size=None) -> Staged:
    return Staged(inputs=inputs, params=params or {}, size=size)


I2V_PARAMS = {"camera_fixed": True, "seed": 7}


def i2v_inputs(image):
    return {"image": image, "prompt": PROMPT}


# ---- Seedance 2.5 on Atlas ---------------------------------------------------------------------------------


def test_seedance_i2v_body_matches_recording(manifests):
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(staged(i2v_inputs(MEDIA + "start_a.png"), I2V_PARAMS))
    assert job == "pred_vid_a1"
    body = sent(ft, "/model/generateVideo")
    assert body["model"] == "bytedance/seedance-2.5/image-to-video"
    assert body["image"] == MEDIA + "start_a.png"
    assert "images" not in body and "reference_images" not in body  # a plain i2v carries no reference arrays
    assert body["duration"] == 5 and isinstance(body["duration"], int)
    assert body["resolution"] == "480P"
    assert body["ratio"] == "adaptive"  # edits follow the supplied frame
    assert body["camera_fixed"] is True and body["generate_audio"] is False and body["seed"] == 7
    assert header(ft, "/model/generateVideo", "authorization") == f"Bearer {ATLAS_KEY}"


def test_seedance_poll_records_24_fps_and_8_bit(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(staged(i2v_inputs(MEDIA + "start_a.png"), I2V_PARAMS))
    first = ad.poll(job)
    assert first.state == "running" and first.outputs == []
    done = ad.poll(job)
    assert done.state == "done"
    assert done.outputs == [OSS + "a1.mp4"]
    assert done.output_fps == 24.0
    assert done.output_bits == 8
    assert ad.fetch(done.outputs[0]) == CLIP_A


def test_seedance_1080p_is_10_bit_and_resolution_is_upper_cased(manifests):
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(
        staged(
            i2v_inputs(MEDIA + "start_a.png"),
            {"duration": 8, "resolution": "1080p", "camera_fixed": True, "seed": 7},
        )
    )
    assert sent(ft, "/model/generateVideo")["resolution"] == "1080P"
    status = finish(ad, job)
    assert (status.output_fps, status.output_bits) == (24.0, 10)


def test_seedance_bits_survive_a_restart_when_the_runner_supplies_the_resolution(manifests):
    first, _ = atlas(manifests, "seedance-2.5-i2v")
    job = first.submit(
        staged(
            i2v_inputs(MEDIA + "start_a.png"),
            {"duration": 8, "resolution": "1080P", "camera_fixed": True, "seed": 7},
        )
    )
    fresh, _ = atlas(manifests, "seedance-2.5-i2v")  # a new process knows nothing about the job
    fresh.poll(job)
    assert fresh.poll(job).output_bits is None
    again, _ = atlas(manifests, "seedance-2.5-i2v")
    again.poll(job)
    assert again.poll(job, resolution="1080P").output_bits == 10


def test_seedance_local_file_goes_through_upload_media(manifests, tmp_path):
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    path = still_file(tmp_path, "start_a.png", STILL_A)
    job = ad.submit(staged(i2v_inputs(str(path)), I2V_PARAMS))
    assert job == "pred_vid_up"
    assert [c.path for c in ft.served] == ["/api/v1/model/uploadMedia", "/api/v1/model/generateVideo"]
    assert sent(ft, "/model/generateVideo")["image"] == MEDIA + "uploaded_start_a.png"


def test_seedance_r2v_body_has_roles_videos_and_timing_hints(manifests):
    ad, ft = atlas(manifests, "seedance-2.5-r2v")
    prompt = timing_hints(
        "Wide establishing view of the lobby.",
        [(6, "a visitor crosses to the desk"), (0, "the lift doors open")],
        8,
    )
    inputs = {
        "reference_images": [
            {"url": MEDIA + "ref_lobby.png", "role": "first_frame"},
            {"url": MEDIA + "ref_desk.png", "role": "reference_image"},
        ],
        "reference_videos": [MEDIA + "ref_walk.mp4"],
        "prompt": prompt,
    }
    job = ad.submit(
        staged(inputs, {"duration": 8, "resolution": "720P", "ratio": "16:9", "camera_fixed": True})
    )
    assert job == "pred_r2v_1"
    body = sent(ft, "/model/generateVideo")
    assert body["model"] == "bytedance/seedance-2.5/reference-to-video"
    assert body["reference_images"] == [
        {"url": MEDIA + "ref_lobby.png", "role": "first_frame"},
        {"url": MEDIA + "ref_desk.png", "role": "reference_image"},
    ]
    assert body["reference_videos"] == [MEDIA + "ref_walk.mp4"]
    assert body["prompt"].splitlines()[1:] == ["0s: the lift doors open", "6s: a visitor crosses to the desk"]
    assert finish(ad, job).state == "done"


def test_seedance_r2v_caps_roles_and_no_text_to_video(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-r2v")

    def ref(i, role=None):
        return {"url": f"{MEDIA}r{i}.png", **({"role": role} if role else {})}

    ok = {"reference_images": [ref(i) for i in range(30)], "prompt": "p"}
    assert ad.quote(ok, {}).price_usd > 0
    with pytest.raises(InputError, match="cap is 30"):
        ad.quote({"reference_images": [ref(i) for i in range(31)], "prompt": "p"}, {})
    with pytest.raises(InputError, match="cap is 10"):
        ad.quote({**ok, "reference_videos": [f"{MEDIA}v{i}.mp4" for i in range(11)]}, {})
    with pytest.raises(InputError, match="text-to-video"):
        ad.quote({"reference_images": [], "prompt": "p"}, {})
    with pytest.raises(InputError, match="role"):
        ad.quote({"reference_images": [ref(0, "style")], "prompt": "p"}, {})
    with pytest.raises(InputError, match="first_frame"):
        ad.quote({"reference_images": [ref(0, "first_frame"), ref(1, "first_frame")], "prompt": "p"}, {})


def test_timing_hints_are_integer_seconds_inside_the_clip():
    assert timing_hints("Base.", [(0, "a"), (6.0, "b")], 8) == "Base.\n0s: a\n6s: b"
    assert timing_hints("Base.", [], 8) == "Base."
    with pytest.raises(InputError, match="outside"):
        timing_hints("Base.", [(9, "late")], 8)
    with pytest.raises(InputError, match="outside"):
        timing_hints("Base.", [(-1, "early")], 8)


def test_no_text_to_video_on_any_engine(manifests):
    for engine in ("seedance-2.5-i2v", "kling-o3-4k", "kling-v3-pro"):
        ad, _ = atlas(manifests, engine)
        with pytest.raises(InputError, match="image"):
            ad.quote({"prompt": "a lobby at dusk"}, {})
        with pytest.raises(InputError, match="prompt"):
            ad.quote({"image": MEDIA + "start_a.png", "prompt": "  "}, {})


# ---- Kling ---------------------------------------------------------------------------------------------------


def test_kling_o3_start_end_body(manifests):
    ad, ft = atlas(manifests, "kling-o3-4k")
    inputs = {
        "image": {"url": MEDIA + "start_a.png", "w": 1920, "h": 1080},
        "image_tail": MEDIA + "end_a.png",
        "prompt": "A slow push in to the dining table; the light gently shifts to dusk.",
        "elements": [{"image_url": MEDIA + "ref_sofa.png", "name": "sofa"}],
    }
    params = {
        "duration": ad.clamp_duration(5.2),
        "negative_prompt": "morphing, warping, extra people",
        "resolution": "4K",
    }
    job = ad.submit(staged(inputs, params))
    assert job == "pred_kling_o3"
    body = sent(ft, "/model/generateVideo")
    assert body["model"] == "kwaivgi/kling-video-o3-4k/image-to-video"
    assert body["image"] == MEDIA + "start_a.png" and body["image_tail"] == MEDIA + "end_a.png"
    assert (
        body["prompt"] == "A push in to the dining table; the light shifts to dusk."
    )  # pace words gone under image_tail
    assert body["duration"] == 6 and isinstance(body["duration"], int)
    assert body["ratio"] == "16:9"  # from the source dimensions
    assert body["elements"] == [{"image_url": MEDIA + "ref_sofa.png", "name": "sofa"}]
    assert body["negative_prompt"] == "morphing, warping, extra people"
    assert "resolution" not in body  # 4K is the slug; the Atlas client only knows NNNP values
    status = finish(ad, job)
    assert status.state == "done" and status.output_fps is None  # Kling does not report a frame rate here
    assert ad.fetch(status.outputs[0]) == CLIP_A


def test_kling_v3_without_end_frame_keeps_the_prompt_and_follows_a_portrait_source(manifests):
    ad, ft = atlas(manifests, "kling-v3-pro")
    inputs = {
        "image": {"url": MEDIA + "start_portrait.png", "w": 1080, "h": 1920},
        "prompt": "A slow dolly toward the balcony door.",
    }
    job = ad.submit(staged(inputs))
    assert job == "pred_kling_v3"
    body = sent(ft, "/model/generateVideo")
    assert body["model"] == "kwaivgi/kling-v3.0-pro/image-to-video"
    assert body["prompt"] == "A slow dolly toward the balcony door."  # the prompt law needs an end frame
    assert "image_tail" not in body and "negative_prompt" not in body and "elements" not in body
    assert body["ratio"] == "9:16" and body["resolution"] == "1080P" and body["duration"] == 5


def test_kling_ratio_comes_from_the_source_never_a_default(manifests):
    ad, _ = atlas(manifests, "kling-v3-pro")
    inputs = {"image": MEDIA + "start_portrait.png", "prompt": "A slow dolly toward the balcony door."}
    with pytest.raises(InputError, match="source dimensions"):
        ad.submit(staged(inputs))  # nothing says how big the frame is: no guessed ratio
    q = ad.quote(inputs, {})
    assert q.size.label == "auto"  # and no invented frame in the quote either
    assert ad.ratio_for({}, (1920, 1080)) == "16:9" and ad.ratio_for({}, (1080, 1920)) == "9:16"
    assert ad.ratio_for({}, (1000, 1000)) == "16:9" and ad.ratio_for({}, None) is None
    # the size egress.prepare chose is a source too
    assert ad.submit(staged(inputs, size=EngineSize(w=1080, h=1920))) == "pred_kling_v3"


def test_kling_quote_reads_dimensions_from_a_png_header(manifests, tmp_path):
    ad, _ = atlas(manifests, "kling-v3-pro")
    path = still_file(tmp_path, "portrait.png", STILL_PORTRAIT)
    q = ad.quote({"image": str(path), "prompt": "p"}, {})
    assert (q.size.w, q.size.h) == (1080, 1920)


def test_duration_is_clamped_by_ceil_to_each_engines_range(manifests):
    kling, _ = atlas(manifests, "kling-v3-pro")
    assert [kling.clamp_duration(x) for x in (0.2, 5.0, 5.2, 14.01, 15.0, 40)] == [1, 5, 6, 15, 15, 15]
    seed, _ = atlas(manifests, "seedance-2.5-i2v")
    assert [seed.clamp_duration(x) for x in (2, 4, 7.01, 30, 31)] == [4, 4, 8, 30, 30]
    veo = VertexVeo(manifests["veo-3.1"], transport=fixtures("vertex"))
    assert [veo.clamp_duration(x) for x in (0, 4.5, 8, 12)] == [4, 5, 8, 8]


def test_out_of_range_or_fractional_duration_is_a_param_error(manifests):
    kling, _ = atlas(manifests, "kling-o3-4k")
    for bad in (0, 16, 5.5):
        with pytest.raises(ParamError):
            kling.validate_params({"duration": bad})
    assert kling.validate_params({"duration": 15.0})["duration"] == 15  # a whole float is an int
    seed, _ = atlas(manifests, "seedance-2.5-r2v")
    for bad in (3, 31):
        with pytest.raises(ParamError):
            seed.validate_params({"duration": bad})


def test_negative_prompt_is_limited_to_29_words(manifests):
    ad, _ = atlas(manifests, "kling-o3-4k")
    assert ad.validate_params({"negative_prompt": " ".join(["bad"] * 29)})["negative_prompt"]
    with pytest.raises(ParamError, match="under 30"):
        ad.validate_params({"negative_prompt": " ".join(["bad"] * 30)})


def test_resolution_case_is_normalised_then_checked_against_the_enum(manifests):
    seed, _ = atlas(manifests, "seedance-2.5-i2v")
    for spelling in ("1080p", "1080P", 1080, "1080"):
        assert seed.validate_params({"resolution": spelling})["resolution"] == "1080P"
    with pytest.raises(ParamError):
        seed.validate_params({"resolution": "2160P"})  # Seedance 2.5 has no 4K
    kling, _ = atlas(manifests, "kling-o3-4k")
    assert kling.validate_params({"resolution": "4k"})["resolution"] == "4K"
    with pytest.raises(ParamError):
        kling.validate_params({"resolution": "8K"})


def test_unknown_params_are_rejected_on_every_adapter(manifests):
    seed, _ = atlas(manifests, "seedance-2.5-i2v")
    with pytest.raises(ParamError, match="motion_strength"):
        seed.validate_params({"motion_strength": 3})
    veo = VertexVeo(manifests["veo-3.1"], transport=fixtures("vertex"))
    with pytest.raises(ParamError, match="fps"):
        veo.validate_params({"fps": 24})
    wan = WanVace(manifests["wan-2.2-vace"])
    with pytest.raises(ParamError, match="duration"):
        wan.validate_params({"duration": 4})  # Wan's parameter is duration_s


# ---- quotes -----------------------------------------------------------------------------------------------


def test_quotes_come_from_manifest_data(manifests):
    seed, ft = atlas(manifests, "seedance-2.5-i2v")
    q = seed.quote(
        {"image": {"url": MEDIA + "start_a.png", "w": 1920, "h": 1080}, "prompt": PROMPT},
        {"duration": 8, "resolution": "1080P"},
    )
    assert q.engine_id == "seedance-2.5-i2v" and q.route_id == "atlas"
    assert q.price_usd == pytest.approx(0.10 * 8)
    assert q.price_status == "unconfirmed"
    assert q.custody_grade == "D"  # public OSS uploads
    assert q.model_version == "bytedance/seedance-2.5/image-to-video"
    assert (q.size.w, q.size.h, q.size.label) == (1920, 1080, "1080P")
    assert q.eta_s > 0
    assert ft.served == []  # a quote makes no request

    o3, _ = atlas(manifests, "kling-o3-4k")
    q = o3.quote(
        {"image": {"url": MEDIA + "start_a.png", "w": 1920, "h": 1080}, "prompt": "p"},
        {"duration": 5, "resolution": "4K"},
    )
    assert q.price_usd == pytest.approx(0.357 * 5)
    assert (q.size.w, q.size.h, q.size.label) == (3840, 2160, "4K")


def test_every_video_engine_quotes_a_grade_and_never_below_the_manifest(manifests, tmp_path):
    still = still_file(tmp_path, "still_a.png", STILL_A)
    veo = VertexVeo(manifests["veo-3.1"], transport=fixtures("vertex"))
    q = veo.quote({"start_image": str(still), "prompt": VEO_PROMPT}, {})
    assert (q.route_id, q.custody_grade) == ("vertex", "D")  # C1 candidate, terms pending: treated as D
    assert q.price_usd == pytest.approx(0.21 * 8)
    wan = WanVace(manifests["wan-2.2-vace"])
    q = wan.quote({"reference_images": [str(still)], "prompt": "p"}, {})
    assert (q.route_id, q.custody_grade, q.price_usd) == ("local", "C0", 0.0)


def test_frame_size_follows_the_source_for_adaptive_and_is_auto_when_unknown():
    assert frame_size("1080P", "adaptive", (1920, 1080)).model_dump() == {
        "w": 1920,
        "h": 1080,
        "label": "1080P",
    }
    assert frame_size("720P", "adaptive", (1080, 1920)).model_dump() == {"w": 720, "h": 1280, "label": "720P"}
    assert frame_size("1080P", "21:9", (1920, 1080)).w == 2520
    assert frame_size("1080P", "adaptive", None).label == "auto"
    assert frame_size(None, "16:9", (1920, 1080)).label == "auto"


# ---- construction, hosts, keys ---------------------------------------------------------------------------


def test_cloud_adapters_refuse_to_exist_without_a_transport(manifests):
    for engine in ("seedance-2.5-i2v", "seedance-2.5-r2v", "kling-o3-4k", "kling-v3-pro"):
        with pytest.raises(MissingTransport):
            atlas_video_adapter(manifests[engine])
    with pytest.raises(MissingTransport):
        VertexVeo(manifests["veo-3.1"])
    with pytest.raises(MissingTransport):
        BytePlusSeedance(manifests["seedance-2.5-i2v"])
    with pytest.raises(MissingTransport):
        WaveSpeedSeedance(manifests["seedance-2.5-i2v"])
    wan = WanVace(manifests["wan-2.2-vace"])  # local: no HTTP at all
    with pytest.raises(MissingTransport, match="local route"):
        wan.http()


def test_an_adapter_only_drives_its_own_engines_and_provider(manifests):
    ft = fixtures("atlas_video")
    with pytest.raises(ValueError, match="drives"):
        SeedanceI2V(manifests["kling-o3-4k"], None, ft)
    with pytest.raises(ValueError, match="no Atlas video adapter"):
        atlas_video_adapter(manifests["veo-3.1"], None, ft)
    with pytest.raises(ValueError, match="drives"):
        BytePlusSeedance(manifests["kling-o3-4k"], "byteplus", ft)  # Kling is not a Seedance engine
    with pytest.raises(ValueError, match="speaks to"):
        SeedanceR2V(
            manifests["seedance-2.5-r2v"], "byteplus", ft
        )  # the Atlas adapter on another provider's route
    with pytest.raises(ValueError, match="speaks to"):
        BytePlusSeedance(manifests["seedance-2.5-i2v"], "wavespeed", ft)
    with pytest.raises(ValueError, match="no route"):
        SeedanceI2V(manifests["seedance-2.5-i2v"], "nonesuch", ft)


def test_a_missing_key_fails_before_anything_is_sent(manifests, monkeypatch):
    monkeypatch.delenv("ATLASCLOUD_KEY")
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    with pytest.raises(AtlasError, match="ATLASCLOUD_KEY"):
        ad.submit(staged(i2v_inputs(MEDIA + "start_a.png"), I2V_PARAMS))
    assert ft.served == []


def test_downloads_are_confined_to_result_hosts(manifests):
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    for url in (
        "https://evil.example.com/x.mp4",
        "https://dl.example.aliyuncs.com.evil.example.com/x.mp4",
        "https://atlascloud.ai.evil.example.com/x.mp4",
    ):
        with pytest.raises(FetchError, match="not an Atlas result host"):
            ad.fetch(url)
    assert ft.served == []
    with pytest.raises(HostNotAllowed):
        ad.http().get("https://evil.example.com/x.mp4")  # the allowlist wraps every request the adapter makes


def test_an_unrecorded_request_never_reaches_the_network(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    with pytest.raises(UnrecordedRequest):
        ad.submit(staged(i2v_inputs(MEDIA + "never_recorded.png"), I2V_PARAMS))


# ---- refusals -----------------------------------------------------------------------------------------------


def test_refused_job_ids_carry_their_message_and_survive_a_restart(manifests):
    job = refused_job_id("blocked: ünïcode")
    assert refusal_from_job_id(job) == "blocked: ünïcode"
    assert refusal_from_job_id("pred_vid_a1") is None
    ad, ft = atlas(manifests, "seedance-2.5-i2v")
    status = ad.poll(job)
    assert status.state == "refused" and "ünïcode" in status.message
    assert ft.served == []  # no request: the id says it all


def test_atlas_moderation_at_submit_reads_as_refused(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(staged(i2v_inputs(MEDIA + "person_moderated.png"), I2V_PARAMS))
    status = ad.poll(job)
    assert status.state == "refused" and "moderation" in status.message


def test_atlas_real_face_rejection_reads_as_refused_at_submit_and_while_running(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    at_submit = ad.submit(staged(i2v_inputs(MEDIA + "person_face.png"), I2V_PARAMS))
    assert ad.poll(at_submit).state == "refused"
    later = ad.submit(staged(i2v_inputs(MEDIA + "person_late.png"), I2V_PARAMS))
    status = ad.poll(later)
    assert status.state == "refused" and "real person" in status.message


def test_a_plain_atlas_failure_stays_failed(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(staged(i2v_inputs(MEDIA + "start_fail.png"), I2V_PARAMS))
    status = ad.poll(job)
    assert status.state == "failed" and "out of memory" in status.message


def test_wait_polls_at_the_video_cadence_and_times_out(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    job = ad.submit(staged(i2v_inputs(MEDIA + "start_a.png"), I2V_PARAMS))
    sleeps: list[float] = []
    status = ad.wait(job, sleep=sleeps.append)
    assert status.state == "done" and sleeps == [5.0, 5.0]
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    pending, _ = atlas(manifests, "seedance-2.5-i2v")
    with pytest.raises(VideoTimeout):
        pending.wait("pred_vid_never_done", sleep=sleep, now=lambda: clock["t"], timeout_s=10)


# ---- output depends on input -------------------------------------------------------------------------------


def test_atlas_output_depends_on_input(manifests):
    ad, _ = atlas(manifests, "seedance-2.5-i2v")
    results = []
    for key in ("a", "b"):
        inputs = i2v_inputs(MEDIA + f"start_{key}.png")
        job = ad.submit(staged(inputs, I2V_PARAMS))
        results.append((inputs_digest(inputs), ad.fetch(finish(ad, job).outputs[0])))
    assert results[0][0] != results[1][0] and results[0][1] != results[1][1]
    check_output_depends_on_input(results)


def test_a_vendor_that_ignores_the_input_is_caught(tmp_path, manifests):
    """Two different first frames both come back as the same clip: the nightly job must raise."""
    url = OSS + "same.mp4"
    for key in ("a", "b"):
        body = {
            "model": "bytedance/seedance-2.5/image-to-video",
            "image": MEDIA + f"start_{key}.png",
            "prompt": PROMPT,
            "duration": 5,
            "resolution": "480P",
            "ratio": "adaptive",
            "camera_fixed": True,
            "generate_audio": False,
            "return_last_frame": False,
            "seed": 7,
        }
        write_recording(
            tmp_path,
            f"submit_{key}",
            method="POST",
            url="https://api.atlascloud.ai/api/v1/model/generateVideo",
            body=body,
            response={"status": 200, "json": {"data": {"id": f"p_{key}"}}},
        )
        write_recording(
            tmp_path,
            f"poll_{key}",
            method="GET",
            url=f"https://api.atlascloud.ai/api/v1/model/prediction/p_{key}",
            response={"status": 200, "json": {"data": {"status": "completed", "outputs": [url]}}},
        )
    write_recording(
        tmp_path, "result", method="GET", url=url, response={"status": 200, "bytes_b64": b64(CLIP_A)}
    )
    ad = atlas_video_adapter(manifests["seedance-2.5-i2v"], None, FixtureTransport(tmp_path))
    results = []
    for key in ("a", "b"):
        inputs = i2v_inputs(MEDIA + f"start_{key}.png")
        results.append(
            (inputs_digest(inputs), ad.fetch(finish(ad, ad.submit(staged(inputs, I2V_PARAMS))).outputs[0]))
        )
    with pytest.raises(SilentlyIgnoredInput):
        check_output_depends_on_input(results)
    check_output_depends_on_input([results[0], results[0]])  # the same input twice is not a finding


def test_inputs_digest_hashes_file_contents_not_paths(tmp_path):
    a = still_file(tmp_path, "one.png", STILL_A)
    b = still_file(tmp_path, "two.png", STILL_A)
    c = still_file(tmp_path, "three.png", STILL_B)
    assert inputs_digest({"image": str(a), "prompt": "p"}) == inputs_digest({"image": str(b), "prompt": "p"})
    assert inputs_digest({"image": str(a)}) != inputs_digest({"image": str(c)})
    assert inputs_digest({"image": str(a), "prompt": "p"}) != inputs_digest({"image": str(a), "prompt": "q"})


# ---- input parsing ----------------------------------------------------------------------------------------


def test_parse_ref_rules(tmp_path):
    path = still_file(tmp_path, "start_a.png", STILL_A)
    ref = parse_ref(str(path), "image")
    assert ref.path == path and ref.size == (16, 9) and ref.url is None
    assert parse_ref({"path": str(path), "w": 640, "h": 360}, "image").size == (
        640,
        360,
    )  # explicit beats sniffed
    assert (
        parse_ref({"url": MEDIA + "x.png", "role": "first_frame", "name": "hero"}, "image").role
        == "first_frame"
    )
    with pytest.raises(InputError, match="plain http"):
        parse_ref("http://example.com/x.png", "image")
    with pytest.raises(InputError, match="not found"):
        parse_ref(str(tmp_path / "missing.png"), "image")
    with pytest.raises(InputError, match="exactly one"):
        parse_ref({"url": MEDIA + "x.png", "path": str(path)}, "image")
    with pytest.raises(InputError, match="positive"):
        parse_ref({"url": MEDIA + "x.png", "w": 0, "h": 10}, "image")


def test_sniff_image_size_reads_png_and_jpeg_headers_only(tmp_path):
    from PIL import Image

    assert sniff_image_size(still_file(tmp_path, "a.png", STILL_PORTRAIT)) == (9, 16)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buf, "JPEG")
    assert sniff_image_size(still_file(tmp_path, "a.jpg", buf.getvalue())) == (40, 30)
    assert sniff_image_size(still_file(tmp_path, "a.bin", b"not an image at all")) is None
    assert sniff_image_size(still_file(tmp_path, "cut.png", STILL_A[:20])) is None
    assert sniff_image_size(tmp_path / "missing.png") is None
    # the solid PNG helper really is a PNG
    assert Image.open(io.BytesIO(STILL_A)).size == (16, 9)


# ---- BytePlus and WaveSpeed: same model, other provider -------------------------------------------------------


def pair(manifests, engine="seedance-2.5-i2v", provider="byteplus", folder=None):
    ft = fixtures("atlas_video")
    primary = atlas_video_adapter(manifests[engine], None, ft)
    cls = {"byteplus": BytePlusSeedance, "wavespeed": WaveSpeedSeedance}[provider]
    other = fixtures(folder or provider)
    return primary, cls(manifests[engine], provider, other), other


def test_the_fallback_serves_the_same_model_id(manifests):
    primary, byteplus, _ = pair(manifests)
    fb = Fallback(primary, byteplus)
    assert fb.model_id == primary.manifest.model_id == "dreamina-seedance-2-5-260628"
    _, wavespeed, _ = pair(manifests, provider="wavespeed")
    assert Fallback(primary, wavespeed).model_id == primary.manifest.model_id


def test_seedance_fallbacks_lists_exactly_the_declared_active_routes(manifests):
    primary, _, other = pair(manifests)
    found = seedance_fallbacks.seedance_fallbacks(primary, other)
    assert sorted(fb.adapter.route.provider for fb in found) == ["byteplus", "wavespeed"]
    assert all(fb.model_id == primary.manifest.model_id for fb in found)
    assert {type(fb.adapter) for fb in found} == {BytePlusSeedance, WaveSpeedSeedance}
    # a retired route is not offered
    m = manifests["seedance-2.5-i2v"]
    retired = m.model_copy(
        update={
            "routes": [
                r.model_copy(update={"status": "retired"}) if r.provider == "wavespeed" else r
                for r in m.routes
            ]
        }
    )
    again = seedance_fallbacks.seedance_fallbacks(
        atlas_video_adapter(retired, None, fixtures("atlas_video")), other
    )
    assert [fb.adapter.route.provider for fb in again] == ["byteplus"]
    # Kling has no fallback route at all
    kling = atlas_video_adapter(manifests["kling-o3-4k"], None, fixtures("atlas_video"))
    assert seedance_fallbacks.seedance_fallbacks(kling, other) == []


def test_illegal_fallback_pairs_are_rejected(manifests):
    primary, byteplus, other = pair(manifests)
    _, wavespeed, _ = pair(manifests, provider="wavespeed")
    with pytest.raises(FallbackError, match="different provider"):
        Fallback(primary, primary)
    with pytest.raises(FallbackError, match="not declared a same-model fallback"):
        Fallback(byteplus, wavespeed)  # both are fallbacks of atlas, not of each other
    m = manifests["seedance-2.5-i2v"]
    other_model = BytePlusSeedance(m.model_copy(update={"model_id": "some-other-model"}), "byteplus", other)
    with pytest.raises(FallbackError, match="same model_id"):
        Fallback(primary, other_model)
    r2v = BytePlusSeedance(manifests["seedance-2.5-r2v"], "byteplus", other)
    with pytest.raises(FallbackError, match="same engine"):
        Fallback(primary, r2v)


def test_requote_needed_rule():
    from plate_engines.adapters.base import Quote

    def quote(price, route="atlas", version="v1", engine="e"):
        return Quote(
            engine_id=engine,
            route_id=route,
            price_usd=price,
            eta_s=1,
            custody_grade="D",
            size=EngineSize(w=0, h=0, label="auto"),
            model_version=version,
        )

    base = quote(1.00)
    assert requote_needed(base, quote(1.04)) is False
    assert requote_needed(base, quote(1.06)) is True and requote_needed(base, quote(0.90)) is True
    assert requote_needed(base, quote(1.00, version="v2")) is True
    assert (
        requote_needed(base, quote(1.00, route="byteplus")) is True
    )  # another provider always needs a human
    assert requote_needed(quote(0.0), quote(0.0)) is False and requote_needed(quote(0.0), quote(0.1)) is True


def test_a_fallback_cannot_submit_before_a_new_quote_is_approved(manifests, tmp_path, monkeypatch):
    primary, byteplus, other = pair(manifests)
    fb = Fallback(primary, byteplus)
    still = still_file(tmp_path, "start_a.png", STILL_A)
    inputs, params = i2v_inputs(str(still)), I2V_PARAMS
    old = primary.quote(inputs, params)
    calls = []

    def spy(o, n):
        calls.append((o, n))
        return requote_needed(o, n)

    monkeypatch.setattr(
        seedance_fallbacks, "requote_needed", spy
    )  # the default rule is looked up at call time
    quoted = fb.requote(inputs, params, old)
    assert len(calls) == 1 and calls[0][0] is old and calls[0][1] is quoted.quote
    assert quoted.requote_needed is True
    assert quoted.quote.route_id == "byteplus" and quoted.quote.custody_grade == "D"
    assert quoted.quote.price_usd == pytest.approx(0.134 * 5)  # the fallback's own price, not the primary's
    assert quoted.quote.price_usd != old.price_usd

    with pytest.raises(RequoteRequired, match="approve"):
        fb.submit(staged(inputs, params), quoted)
    assert other.served == []  # nothing was sent
    stranger = Fallback(primary, pair(manifests)[1]).requote(inputs, params, old)
    with pytest.raises(RequoteRequired, match="different fallback"):
        fb.submit(staged(inputs, params), stranger, approved=True)
    assert other.served == []

    assert fb.submit(staged(inputs, params), quoted, approved=True) == "cgt-fixture-a"
    assert len(other.served) == 1


def test_a_caller_supplied_rule_can_clear_the_requote_flag(manifests, tmp_path):
    primary, byteplus, other = pair(manifests)
    fb = Fallback(primary, byteplus)
    still = still_file(tmp_path, "start_a.png", STILL_A)
    inputs = i2v_inputs(str(still))
    seen = []
    quoted = fb.requote(
        inputs, I2V_PARAMS, primary.quote(inputs, I2V_PARAMS), check=lambda o, n: seen.append(1) or False
    )
    assert seen == [1] and quoted.requote_needed is False
    assert fb.submit(staged(inputs, I2V_PARAMS), quoted) == "cgt-fixture-a"


def test_byteplus_request_and_result(manifests, tmp_path):
    _, ad, ft = pair(manifests)
    still = still_file(tmp_path, "start_a.png", STILL_A)
    job = ad.submit(staged(i2v_inputs(str(still)), I2V_PARAMS))
    assert job == "cgt-fixture-a"
    call = next(c for c in ft.served if c.method == "POST")
    assert (
        call.host == "ark.ap-southeast.bytepluses.com" and call.path == "/api/v3/contents/generations/tasks"
    )
    assert call.headers["authorization"] == f"Bearer {ARK_KEY}"
    body = json.loads(call.body)
    assert body["model"] == "dreamina-seedance-2-5-260628"
    assert body["content"][1] == {
        "type": "image_url",
        "image_url": {"url": data_url(STILL_A)},
        "role": "first_frame",
    }
    assert body["resolution"] == "480p" and body["ratio"] == "adaptive" and body["duration"] == 5
    assert ad.poll(job).state == "running"
    done = ad.poll(job)
    assert done.state == "done" and done.outputs == [TOS + "a.mp4"]
    assert (done.output_fps, done.output_bits) == (24.0, 8)
    assert done.usage["total_tokens"] == 108900
    assert ad.fetch(done.outputs[0]) == CLIP_A
    with pytest.raises(HostNotAllowed):
        ad.fetch("https://evil.example.com/x.mp4")


def test_byteplus_output_depends_on_input(manifests, tmp_path):
    _, ad, _ = pair(manifests)
    results = []
    for key, data in (("a", STILL_A), ("b", STILL_B)):
        inputs = i2v_inputs(str(still_file(tmp_path, f"start_{key}.png", data)))
        job = ad.submit(staged(inputs, I2V_PARAMS))
        results.append((inputs_digest(inputs), ad.fetch(finish(ad, job).outputs[0])))
    assert results[0][1] == CLIP_A and results[1][1] == CLIP_B
    check_output_depends_on_input(results)


def test_byteplus_refusals(manifests, tmp_path):
    _, ad, _ = pair(manifests)
    at_submit = ad.submit(staged(i2v_inputs(str(still_file(tmp_path, "p.png", STILL_PORTRAIT))), I2V_PARAMS))
    status = ad.poll(at_submit)
    assert status.state == "refused" and "real person" in status.message
    late = ad.submit(staged(i2v_inputs(str(still_file(tmp_path, "e.png", STILL_END))), I2V_PARAMS))
    status = ad.poll(late)
    assert status.state == "refused" and "OutputVideoSensitiveContentDetected" in status.message


def test_byteplus_needs_its_key_and_inline_stills_and_https_videos(manifests, tmp_path, monkeypatch):
    _, ad, _ = pair(manifests)
    still = still_file(tmp_path, "start_a.png", STILL_A)
    monkeypatch.delenv("ARK_API_KEY")
    with pytest.raises(seedance_fallbacks.VideoSubmitError, match="ARK_API_KEY"):
        ad.submit(staged(i2v_inputs(str(still)), I2V_PARAMS))
    monkeypatch.setenv("ARK_API_KEY", ARK_KEY)
    ref = BytePlusSeedance(manifests["seedance-2.5-r2v"], "byteplus", fixtures("byteplus"))
    video = still_file(tmp_path, "walk.mp4", CLIP_A)
    with pytest.raises(InputError, match="https URLs"):
        ref.submit(
            staged({"reference_images": [str(still)], "reference_videos": [str(video)], "prompt": "p"}, {})
        )


def test_wavespeed_request_and_result(manifests, tmp_path):
    _, ad, ft = pair(manifests, provider="wavespeed")
    still = still_file(tmp_path, "start_a.png", STILL_A)
    job = ad.submit(staged(i2v_inputs(str(still)), I2V_PARAMS))
    assert job == "ws-fixture-a"
    call = next(c for c in ft.served if c.method == "POST")
    assert call.host == "api.wavespeed.ai" and call.path == "/api/v3/bytedance/seedance-v2.5/image-to-video"
    assert call.headers["authorization"] == f"Bearer {WAVESPEED_KEY}"
    body = json.loads(call.body)
    assert body["image"] == data_url(STILL_A) and body["resolution"] == "480p"
    assert "aspect_ratio" not in body  # adaptive is WaveSpeed's default: follow the image
    assert ad.model_version == "bytedance/seedance-v2.5/image-to-video"
    assert ad.poll(job).state == "running"
    done = ad.poll(job)
    assert done.state == "done" and done.outputs == [WS_CDN + "a.mp4"]
    assert (done.output_fps, done.output_bits) == (24.0, 8)
    assert ad.fetch(done.outputs[0]) == CLIP_A


def test_wavespeed_body_names_a_non_adaptive_ratio_and_r2v_references(manifests, tmp_path):
    ft = fixtures("wavespeed")
    ad = WaveSpeedSeedance(manifests["seedance-2.5-r2v"], "wavespeed", ft)
    still = still_file(tmp_path, "start_a.png", STILL_A)
    inputs = {
        "reference_images": [{"path": str(still), "role": "first_frame"}],
        "reference_videos": [MEDIA + "ref_walk.mp4"],
        "prompt": "p",
    }
    p = ad.validate_params({"ratio": "9:16", "duration": 6, "resolution": "720P"})
    body = ad.build_body(ad.parse_inputs(inputs), p)
    assert body["aspect_ratio"] == "9:16" and body["resolution"] == "720p" and body["duration"] == 6
    assert body["reference_images"] == [{"url": data_url(STILL_A), "role": "first_frame"}]
    assert body["reference_videos"] == [MEDIA + "ref_walk.mp4"]
    assert ad.model_version == "bytedance/seedance-v2.5/reference-to-video"
    assert ft.served == []


def test_wavespeed_refusals_and_failures(manifests, tmp_path):
    _, ad, _ = pair(manifests, provider="wavespeed")
    blocked = ad.submit(staged(i2v_inputs(str(still_file(tmp_path, "p.png", STILL_PORTRAIT))), I2V_PARAMS))
    assert ad.poll(blocked).state == "refused"
    late = ad.submit(staged(i2v_inputs(str(still_file(tmp_path, "e.png", STILL_END))), I2V_PARAMS))
    status = ad.poll(late)
    assert status.state == "refused" and "safety" in status.message


def test_fallback_adapters_apply_the_same_validation_as_the_primary(manifests):
    primary, byteplus, _ = pair(manifests)
    for ad in (primary, byteplus):
        with pytest.raises(ParamError):
            ad.validate_params({"duration": 31})
        with pytest.raises(InputError, match="image"):
            ad.quote({"prompt": "p"}, {})
    assert byteplus.quote({"image": MEDIA + "start_a.png", "prompt": "p"}, {}).route_id == "byteplus"


# ---- Vertex Veo 3.1 --------------------------------------------------------------------------------------


def veo(manifests, **kw):
    ft = fixtures("vertex")
    return VertexVeo(manifests["veo-3.1"], transport=ft, **kw), ft


def veo_url(region, verb):
    return f"/v1/projects/{PROJECT}/locations/{region}/publishers/google/models/veo-3.1-generate-001:{verb}"


def test_vertex_submit_body_and_headers(manifests, tmp_path):
    ad, ft = veo(manifests)
    still = still_file(tmp_path, "still_a.png", STILL_A)
    job = ad.submit(staged({"start_image": str(still), "prompt": VEO_PROMPT}))
    assert job.endswith("/operations/op-a")
    call = ft.served[0]
    assert call.host == "asia-south1-aiplatform.googleapis.com" and call.path == veo_url(
        "asia-south1", "predictLongRunning"
    )
    assert call.headers["authorization"] == f"Bearer {VERTEX_TOKEN}"
    body = json.loads(call.body)
    assert body["instances"][0]["image"] == {"bytesBase64Encoded": b64(STILL_A), "mimeType": "image/png"}
    assert body["instances"][0]["prompt"] == VEO_PROMPT and "lastFrame" not in body["instances"][0]
    assert body["parameters"] == {
        "durationSeconds": 8,
        "aspectRatio": "16:9",
        "resolution": "1080p",
        "generateAudio": False,
        "sampleCount": 1,
    }


def test_vertex_inline_video_is_staged_and_fetched(manifests, tmp_path):
    ad, ft = veo(manifests)
    job = ad.submit(
        staged({"start_image": str(still_file(tmp_path, "still_a.png", STILL_A)), "prompt": VEO_PROMPT})
    )
    assert ad.poll(job).state == "running"
    done = ad.poll(job)
    assert done.state == "done" and done.output_fps == 24.0
    assert len(done.outputs) == 1 and done.outputs[0].startswith("plate-staged://veo-")
    assert ad.fetch(done.outputs[0]) == CLIP_A
    poll_call = [c for c in ft.served if c.path.endswith(":fetchPredictOperation")][0]
    assert json.loads(poll_call.body) == {"operationName": job}
    with pytest.raises(FetchError, match="not staged"):
        ad.fetch("plate-staged://veo-0000000000000000.mp4")
    other, _ = veo(manifests)
    with pytest.raises(FetchError, match="not staged"):
        other.fetch(done.outputs[0])  # staged bytes belong to the adapter that polled them


def test_vertex_output_depends_on_input(manifests, tmp_path):
    ad, _ = veo(manifests)
    results = []
    for key, data in (("a", STILL_A), ("b", STILL_B)):
        inputs = {"start_image": str(still_file(tmp_path, f"still_{key}.png", data)), "prompt": VEO_PROMPT}
        job = ad.submit(staged(inputs))
        results.append((inputs_digest(inputs), ad.fetch(finish(ad, job).outputs[0])))
    assert results[0][1] == CLIP_A and results[1][1] == CLIP_B
    check_output_depends_on_input(results)


def test_vertex_region_flag_url_end_frame_and_gcs_output(manifests, tmp_path):
    default, _ = veo(manifests)
    assert default.flags == ("availability_unverified",)
    assert default.route.custody_status == "pending_written_terms"
    ad, ft = veo(manifests, region="us-central1")
    assert ad.flags == ()  # naming a region clears the unverified-default flag
    inputs = {
        "start_image": str(still_file(tmp_path, "still_a.png", STILL_A)),
        "end_image": str(still_file(tmp_path, "end_a.png", STILL_END)),
        "prompt": VEO_PROMPT,
    }
    job = ad.submit(
        staged(
            inputs,
            {"duration": 6, "resolution": "720P", "negative_prompt": "morphing, extra people", "seed": 11},
        )
    )
    call = ft.served[0]
    assert call.host == "us-central1-aiplatform.googleapis.com" and call.path == veo_url(
        "us-central1", "predictLongRunning"
    )
    body = json.loads(call.body)
    assert body["instances"][0]["lastFrame"] == {
        "bytesBase64Encoded": b64(STILL_END),
        "mimeType": "image/png",
    }
    assert body["parameters"]["resolution"] == "720p" and body["parameters"]["seed"] == 11
    assert (
        body["parameters"]["negativePrompt"] == "morphing, extra people"
        and body["parameters"]["durationSeconds"] == 6
    )
    done = ad.poll(job)
    assert done.state == "done" and done.outputs == ["gs://plate-test-veo/out/c.mp4"]
    assert ad.fetch(done.outputs[0]) == CLIP_B
    media = ft.served[-1]
    assert (
        media.host == "storage.googleapis.com" and media.headers["authorization"] == f"Bearer {VERTEX_TOKEN}"
    )
    with pytest.raises(FetchError, match="malformed"):
        ad.fetch("gs://bucket-only")


def test_vertex_duration_resolution_and_inputs(manifests, tmp_path):
    ad, _ = veo(manifests)
    assert (
        ad.validate_params({"duration": 4})["duration"] == 4
        and ad.validate_params({"duration": 8})["duration"] == 8
    )
    for bad in (3, 9):
        with pytest.raises(ParamError):
            ad.validate_params({"duration": bad})
    assert ad.validate_params({"resolution": "1080P"})["resolution"] == "1080p"
    with pytest.raises(ParamError):
        ad.validate_params({"resolution": "480p"})
    with pytest.raises(InputError, match="local files"):
        ad.submit(staged({"start_image": MEDIA + "start_a.png", "prompt": VEO_PROMPT}))
    with pytest.raises(InputError, match="start_image"):
        ad.quote({"prompt": VEO_PROMPT}, {})


def test_vertex_refusals_failures_and_transient_errors(manifests, tmp_path):
    ad, _ = veo(manifests)

    def run(name, rgb):
        data = png_bytes(16, 9, rgb)
        return ad.submit(staged({"start_image": str(still_file(tmp_path, name, data)), "prompt": VEO_PROMPT}))

    refused = ad.submit(
        staged({"start_image": str(still_file(tmp_path, "p.png", STILL_PORTRAIT)), "prompt": VEO_PROMPT})
    )
    assert ad.poll(refused).state == "refused"
    rai = ad.poll(run("rai.png", (10, 10, 10)))
    assert rai.state == "refused" and "responsible AI" in rai.message
    policy = ad.poll(run("policy.png", (20, 20, 20)))
    assert policy.state == "refused" and "policy" in policy.message
    failed = ad.poll(run("fail.png", (30, 30, 30)))
    assert failed.state == "failed" and "Internal error" in failed.message
    assert ad.poll(run("busy.png", (50, 50, 50))).state == "running"  # a 429 while polling is not a failure


def test_vertex_needs_project_and_token(manifests, tmp_path, monkeypatch):
    still = still_file(tmp_path, "still_a.png", STILL_A)
    inputs = {"start_image": str(still), "prompt": VEO_PROMPT}
    monkeypatch.delenv("VERTEX_ACCESS_TOKEN")
    ad, ft = veo(manifests)
    with pytest.raises(seedance_fallbacks.VideoSubmitError, match="VERTEX_ACCESS_TOKEN"):
        ad.submit(staged(inputs))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    no_project, _ = veo(manifests, token="t")
    with pytest.raises(seedance_fallbacks.VideoSubmitError, match="GOOGLE_CLOUD_PROJECT"):
        no_project.submit(staged(inputs))
    assert ft.served == []
    by_callable, _ = veo(manifests, project=PROJECT, token=lambda: VERTEX_TOKEN)
    assert by_callable.submit(staged(inputs)).endswith("/operations/op-a")


# ---- Wan 2.2 VACE (local, PREVIS always) ----------------------------------------------------------------------


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not on PATH")


def wan(manifests, tmp_path, name="stage"):
    return WanVace(manifests["wan-2.2-vace"], staging_dir=tmp_path / name)


def wan_inputs(tmp_path, data=None, name="wan_still.png"):
    return {
        "reference_images": [str(still_file(tmp_path, name, data or png_bytes(64, 36, (180, 90, 30))))],
        "prompt": "p",
    }


WAN_PARAMS = {"duration_s": 1.0, "fps": 24}


def test_wan_is_previs_always_and_needs_a_gpu_or_the_stub(manifests, tmp_path, monkeypatch):
    assert WanVace.previs is True
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> ImportError, whatever this machine has
    ad = wan(manifests, tmp_path)
    with pytest.raises(LocalModelUnavailable, match="PLATE_STUB_LOCAL"):
        ad.submit(staged(wan_inputs(tmp_path), WAN_PARAMS))
    assert not (tmp_path / "stage").exists()  # nothing was written


@needs_ffmpeg
def test_wan_stub_writes_a_tagged_24_fps_previs_clip(manifests, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    ad = wan(manifests, tmp_path)
    job = ad.submit(staged(wan_inputs(tmp_path), WAN_PARAMS))
    status = ad.poll(job)
    assert status.state == "done" and status.output_fps == 24.0
    assert "PREVIS" in status.message and status.usage == {"previs": True, "stub": True}
    clip = ad.fetch(status.outputs[0])
    assert clip[4:8] == b"ftyp" and b"PREVIS" in clip  # an mp4 whose own tags say PREVIS
    sidecar = json.loads((tmp_path / "stage" / f"{job}.previs.json").read_text(encoding="utf-8"))
    assert sidecar["previs"] is True and sidecar["stub"] is True and sidecar["fps"] == 24
    cv2 = pytest.importorskip("cv2")
    cap = cv2.VideoCapture(str(tmp_path / "stage" / f"{job}.mp4"))
    try:
        assert cap.get(cv2.CAP_PROP_FPS) == pytest.approx(24.0)
        assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 24
        assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (
            sidecar["width"],
            sidecar["height"],
        )
    finally:
        cap.release()


@needs_ffmpeg
def test_wan_stub_pixels_depend_on_the_input_still_and_are_repeatable(manifests, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    first = wan(manifests, tmp_path, "one")
    again = wan(manifests, tmp_path, "two")
    other = wan(manifests, tmp_path, "three")
    inputs_a = wan_inputs(tmp_path)
    inputs_b = wan_inputs(tmp_path, png_bytes(64, 36, (20, 60, 200)), "other_still.png")
    clip_a = first.fetch(first.poll(first.submit(staged(inputs_a, WAN_PARAMS))).outputs[0])
    clip_a2 = again.fetch(again.poll(again.submit(staged(inputs_a, WAN_PARAMS))).outputs[0])
    clip_b = other.fetch(other.poll(other.submit(staged(inputs_b, WAN_PARAMS))).outputs[0])
    assert clip_a == clip_a2  # same inputs, same bytes
    assert clip_a != clip_b  # different still, different clip
    results = [(inputs_digest(inputs_a), clip_a), (inputs_digest(inputs_b), clip_b)]
    check_output_depends_on_input(results)


@needs_ffmpeg
def test_wan_same_job_is_not_rendered_twice(manifests, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    ad = wan(manifests, tmp_path)
    inputs = wan_inputs(tmp_path)
    job = ad.submit(staged(inputs, WAN_PARAMS))

    def boom(*a, **k):
        raise AssertionError("ffmpeg ran again for an identical job")

    with monkeypatch.context() as m:
        m.setattr(local_wan.subprocess, "run", boom)
        assert ad.submit(staged(inputs, WAN_PARAMS)) == job
    # a different seed is a different job, so it does render
    assert ad.submit(staged(inputs, {**WAN_PARAMS, "seed": 5})) != job


def test_wan_poll_of_an_unknown_job_fails_and_fetch_is_confined(manifests, tmp_path):
    ad = wan(manifests, tmp_path)
    assert ad.poll("wan-0000000000000000").state == "failed"
    (tmp_path / "stage").mkdir()
    (tmp_path / "secret.mp4").write_bytes(b"outside")
    (tmp_path / "stage" / "ok.mp4").write_bytes(b"inside")
    assert ad.fetch("plate-staged://ok.mp4") == b"inside"
    for url in (
        "plate-staged://../secret.mp4",
        "plate-staged://sub/ok.mp4",
        "plate-staged://sub\\ok.mp4",
        "plate-staged://C:\\x.mp4",
        "plate-staged://ok.exe",
        "plate-staged://",
        "https://example.com/ok.mp4",
        str(tmp_path / "secret.mp4"),
    ):
        with pytest.raises(FetchError):
            ad.fetch(url)


def test_wan_stub_needs_a_local_still_and_ffmpeg(manifests, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    ad = wan(manifests, tmp_path)
    with pytest.raises(InputError, match="local still"):
        ad.submit(staged({"reference_images": [MEDIA + "x.png"], "prompt": "p"}, WAN_PARAMS))
    monkeypatch.setattr(local_wan.shutil, "which", lambda name: None)
    with pytest.raises(LocalModelUnavailable, match="ffmpeg"):
        ad.submit(staged(wan_inputs(tmp_path), WAN_PARAMS))
    assert not list((tmp_path / "stage").glob("*.part"))  # no half-written clip is left behind


def test_replace_with_retry_survives_a_busy_destination(tmp_path, monkeypatch):
    src, dest = tmp_path / "a.part", tmp_path / "a.mp4"
    src.write_bytes(b"x")
    real, attempts, slept = os.replace, [], []

    def flaky(a, b):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("held by another process")
        real(a, b)

    monkeypatch.setattr(local_wan.os, "replace", flaky)
    monkeypatch.setattr(local_wan.time, "sleep", slept.append)
    local_wan._replace_with_retry(src, dest)
    assert dest.read_bytes() == b"x" and len(attempts) == 3 and slept == [0.05, 0.1]
    monkeypatch.setattr(
        local_wan.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("always"))
    )
    with pytest.raises(PermissionError):
        local_wan._replace_with_retry(src, dest, tries=2)
