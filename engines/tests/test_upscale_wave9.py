"""Wave 9 upscalers (router plan T100, decision 55): Magnific's new address and limits, Real-ESRGAN's Lanczos stub.

Magnific's own documentation moved the API off the Freepik host: api.magnific.com, header x-magnific-api-key,
POST /v1/ai/image-upscaler-precision-v2 (scale_factor 2 to 16, UNCONFIRMED). The recording below is synthetic,
written into tmp_path by the same code the test replays (the T17 pattern for an upscaler with no committed folder),
so a request that stops going where the adapter says fails here. Both upscalers refuse an input over
`limits.max_input_mp` or an output over `limits.max_output_mp` before anything is sent, with an InputError that
names the limit and the size. Nothing here reaches a vendor: conftest.py blocks sockets.
"""

from __future__ import annotations

import base64
import json

import cv2
import httpx
import numpy as np
import pytest

from plate_engines.adapters.base import InputError, PlateUpscaleForbidden, Staged
from plate_engines.adapters.image_common import LocalModelUnavailable
from plate_engines.adapters.transport import FixtureTransport, HostNotAllowed, write_recording
from plate_engines.adapters.upscale import MagnificPrecisionV2, RealESRGAN, check_limits

KEY = "mag-key"
HOST = "api.magnific.com"
PATH = "/v1/ai/image-upscaler-precision-v2"
URL = f"https://{HOST}{PATH}"
RESULT = "https://ai-statics.freepik.com/up/wave9.png"


def png(w: int, h: int, seed: int = 1, *, depth: int = 8) -> bytes:
    rng = np.random.default_rng(seed)
    hi = 256 if depth == 8 else 65536
    arr = rng.integers(0, hi, size=(h, w, 3), dtype=np.uint16 if depth == 16 else np.uint8)
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return bytes(buf)


def flat_png(w: int, h: int) -> bytes:
    """A big, cheap PNG (one colour): only its IHDR is read before a limit refuses it."""
    ok, buf = cv2.imencode(".png", np.zeros((h, w, 3), np.uint8))
    assert ok
    return bytes(buf)


def layer(data: bytes, role: str = "ai_layer") -> dict:
    return {"image": {"bytes": data, "name": "layer.png", "role": role}}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MAGNIFIC_API_KEY", KEY)
    for name in ("FREEPIK_API_KEY", "PLATE_STUB_LOCAL", "PLATE_LIVE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def stage_dir(tmp_path):
    return tmp_path / "staging"


# ---------------------------------------------------------------------------- the manifests


def test_magnific_manifest_names_the_new_host_defaults_range_and_limits(manifests):
    m = manifests["magnific-precision-v2"]
    route = m.routes[0]
    assert route.endpoint == f"https://{HOST}" and ".magnific.com" in route.allowed_hosts
    assert (route.custody_grade, route.custody_status) == ("D", "pending_written_terms")
    params = {p.name: p for p in m.params}
    assert params["scale"].range == [2, 16] and params["scale"].default == 2
    assert (params["sharpen"].default, params["smart_grain"].default, params["ultra_detail"].default) == (
        7,
        7,
        30,
    )
    assert (m.limits.max_input_mp, m.limits.max_output_mp) == (6, 50)
    assert m.plate_upscale_forbidden


def test_realesrgan_manifest_caps_the_output_only(manifests):
    m = manifests["real-esrgan"]
    assert (m.limits.max_input_mp, m.limits.max_output_mp) == (None, 50)
    assert m.routes[0].custody_grade == "C0" and m.plate_upscale_forbidden


def test_the_adapter_constants_are_magnifics():
    assert MagnificPrecisionV2.KEY_HEADER == "x-magnific-api-key"
    assert MagnificPrecisionV2.DEFAULT_BASE == f"https://{HOST}"
    assert MagnificPrecisionV2.PATH == PATH


# ---------------------------------------------------------------------------- Magnific on a recorded fixture


def _record(dest, image: bytes) -> None:
    body = {"image": b64(image), "scale_factor": 2, "sharpen": 7, "smart_grain": 7, "ultra_detail": 30}
    write_recording(
        dest,
        "submit",
        method="POST",
        url=URL,
        body=body,
        response={
            "status": 200,
            "json": {"data": {"task_id": "mag-w9", "status": "CREATED", "generated": []}},
        },
    )
    write_recording(
        dest,
        "poll",
        method="GET",
        url=f"{URL}/mag-w9",
        responses=[
            {
                "status": 200,
                "json": {"data": {"task_id": "mag-w9", "status": "IN_PROGRESS", "generated": []}},
            },
            {
                "status": 200,
                "json": {"data": {"task_id": "mag-w9", "status": "COMPLETED", "generated": [RESULT]}},
            },
        ],
    )
    write_recording(
        dest,
        "download",
        method="GET",
        url=RESULT,
        response={"status": 200, "bytes_b64": b64(png(128, 72, 9))},
    )


def test_magnific_submit_targets_api_magnific_com_with_the_new_header(manifests, tmp_path, stage_dir):
    image = png(64, 36)
    _record(tmp_path / "rec", image)
    replay = FixtureTransport(tmp_path / "rec")
    adapter = MagnificPrecisionV2(manifests["magnific-precision-v2"], None, replay, staging_dir=stage_dir)
    staged = Staged(inputs=layer(image), params=adapter.validate_params({"scale": 2}))
    job = adapter.submit(staged)
    assert job == "mag-w9"
    call = replay.served[0]
    assert (call.method, call.host, call.path) == ("POST", HOST, PATH)
    assert call.headers["x-magnific-api-key"] == KEY
    assert "x-freepik-api-key" not in call.headers
    assert json.loads(call.body) == {
        "image": b64(image),
        "scale_factor": 2,
        "sharpen": 7,
        "smart_grain": 7,
        "ultra_detail": 30,
    }
    assert [adapter.poll(job).state for _ in range(2)] == ["running", "done"]
    poll = replay.served[1]
    assert (poll.method, poll.host, poll.path) == ("GET", HOST, f"{PATH}/mag-w9")
    assert adapter.fetch(adapter.poll(job).outputs[0]) == png(128, 72, 9)  # the result CDN stays allowlisted
    with pytest.raises(HostNotAllowed):
        adapter.fetch("https://cdn.example.net/up.png")


def test_the_freepik_key_still_works_under_the_new_header(manifests, tmp_path, stage_dir, monkeypatch):
    monkeypatch.delenv("MAGNIFIC_API_KEY")
    monkeypatch.setenv("FREEPIK_API_KEY", "old-key")
    image = png(64, 36)
    _record(tmp_path / "rec", image)
    replay = FixtureTransport(tmp_path / "rec")
    adapter = MagnificPrecisionV2(manifests["magnific-precision-v2"], None, replay, staging_dir=stage_dir)
    adapter.submit(Staged(inputs=layer(image), params=adapter.validate_params({"scale": 2})))
    assert replay.served[0].headers["x-magnific-api-key"] == "old-key"


def test_magnific_scale_now_goes_to_16_and_the_plate_is_still_refused(manifests, stage_dir):
    adapter = MagnificPrecisionV2(
        manifests["magnific-precision-v2"],
        None,
        httpx.MockTransport(lambda r: httpx.Response(500)),
        staging_dir=stage_dir,
    )
    q = adapter.quote(layer(png(64, 36)), {"scale": 16})
    assert (q.size.w, q.size.h) == (1024, 576) and q.custody_grade == "D"
    with pytest.raises(PlateUpscaleForbidden):
        adapter.quote(layer(png(64, 36), role="plate"), {"scale": 2})


# ---------------------------------------------------------------------------- limits


def test_a_7_mpx_input_is_refused_naming_the_6_mp_limit_before_anything_is_sent(manifests, stage_dir):
    def boom(request):
        raise AssertionError("an input over the limit must never be sent")

    adapter = MagnificPrecisionV2(
        manifests["magnific-precision-v2"], None, httpx.MockTransport(boom), staging_dir=stage_dir
    )
    big = flat_png(3500, 2000)  # 7.0 MP
    with pytest.raises(InputError, match=r"3500 x 2000 \(7\.00 MP\).*max_input_mp of 6 MP"):
        adapter.quote(layer(big), {"scale": 2})
    with pytest.raises(InputError, match=r"max_input_mp of 6 MP"):
        adapter.submit(Staged(inputs=layer(big), params=adapter.validate_params({"scale": 2})))
    # 6 MP itself is inside the limit.
    assert adapter.quote(layer(flat_png(3000, 2000)), {"scale": 2}).size.w == 6000


def test_an_output_over_50_mp_is_refused_naming_the_limit(manifests, stage_dir):
    adapter = MagnificPrecisionV2(
        manifests["magnific-precision-v2"],
        None,
        httpx.MockTransport(lambda r: httpx.Response(500)),
        staging_dir=stage_dir,
    )
    with pytest.raises(
        InputError, match=r"output would be 12000 x 8000 \(96\.00 MP\), over limits\.max_output_mp of 50 MP"
    ):
        adapter.quote(layer(flat_png(3000, 2000)), {"scale": 4})
    local = RealESRGAN(manifests["real-esrgan"], None, None, staging_dir=stage_dir)
    with pytest.raises(InputError, match=r"max_output_mp of 50 MP"):
        local.quote(layer(flat_png(4000, 3000)), {"scale": 4})
    # Real-ESRGAN has no input cap: a 7 MP layer at 1x is fine.
    assert local.quote(layer(flat_png(3500, 2000)), {"scale": 1}).size.w == 3500


def test_check_limits_without_limits_checks_nothing(manifests):
    m = manifests["gpt-image-2.5"]
    assert m.limits is None
    check_limits(m, (100_000, 100_000), 16)


# ---------------------------------------------------------------------------- the Real-ESRGAN stub


def test_the_realesrgan_stub_is_a_lanczos_resample(manifests, stage_dir, monkeypatch):
    adapter = RealESRGAN(manifests["real-esrgan"], None, None, staging_dir=stage_dir)
    with pytest.raises(LocalModelUnavailable):  # no gpu extra, CUDA or weights on this Mac
        adapter.submit(Staged(inputs=layer(png(64, 36)), params=adapter.validate_params({"scale": 2})))
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    data = png(64, 36, seed=3)
    job = adapter.submit(Staged(inputs=layer(data), params=adapter.validate_params({"scale": 2})))
    status = adapter.poll(job)
    assert status.usage == {"stub": True} and "Lanczos" in status.message
    out = cv2.imdecode(np.frombuffer(adapter.fetch(status.outputs[0]), np.uint8), cv2.IMREAD_UNCHANGED)
    src = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    want = cv2.resize(src, (128, 72), interpolation=cv2.INTER_LANCZOS4)
    assert out.shape == want.shape
    assert int(np.abs(out.astype(np.int16) - want.astype(np.int16)).max()) <= 2


def test_the_stub_keeps_a_16_bit_layer_16_bit(manifests, stage_dir):
    adapter = RealESRGAN(manifests["real-esrgan"], None, None, staging_dir=stage_dir)
    prep = adapter.prepare(layer(png(32, 18, seed=5, depth=16)), {"scale": 3})
    out = cv2.imdecode(np.frombuffer(adapter.stub_upscale(prep), np.uint8), cv2.IMREAD_UNCHANGED)
    assert out.dtype == np.uint16 and out.shape == (54, 96, 3)
