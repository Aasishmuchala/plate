"""Recorded-fixture transport, the host allowlist, the live gate and the Adapter base class.

Nothing here reaches a network: conftest.py turns every socket call into an error, FixtureTransport answers from
recordings, and an unrecorded request raises UnrecordedRequest instead of falling through.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from plate_engines import atlas as atlas_module
from plate_engines.adapters import base
from plate_engines.adapters.base import (
    Adapter,
    AtlasClient,
    FetchError,
    InputError,
    JobStatus,
    MissingTransport,
    ParamError,
    PlateUpscaleForbidden,
    Quote,
    Staged,
)
from plate_engines.adapters.transport import (
    CONTRACT_DIR,
    AllowlistTransport,
    FixtureTransport,
    HostNotAllowed,
    HttpxAtlasTransport,
    LiveDisabled,
    LiveTransport,
    UnrecordedRequest,
    body_key,
    json_body_key,
    load_recordings,
    write_recording,
)
from plate_engines.atlas import AtlasRefused, AtlasTimeout
from plate_engines.models import EngineSize

KEY = "test-key-123"
ATLAS = "https://api.atlascloud.ai/api/v1"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ATLASCLOUD_KEY", KEY)
    monkeypatch.delenv("PLATE_LIVE", raising=False)


def client_for(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


# ---- body keys -------------------------------------------------------------------------------------------


def test_json_body_key_ignores_formatting_and_key_order():
    a = body_key("application/json", b'{"b": 1, "a": [1, 2]}')
    b = body_key("application/json", b'{"a":[1,2],"b":1}')
    assert a == b and len(a) == 8 and set(a) <= set("0123456789abcdef")
    assert json_body_key({"a": [1, 2], "b": 1}) == a
    assert body_key("application/json", b'{"a":[1,3],"b":1}') != a


def test_empty_body_and_binary_body_keys():
    assert body_key("", b"") == body_key("application/json", b"")
    assert body_key("application/octet-stream", b"\x00\x01") != body_key(
        "application/octet-stream", b"\x00\x02"
    )


def test_multipart_key_ignores_the_random_boundary_but_not_the_file():
    def key(payload: bytes, name: str = "plate.png") -> str:
        req = httpx.Request("POST", "https://h/x", files={"file": (name, payload, "image/png")})
        return body_key(req.headers["content-type"], req.read())

    assert key(b"same") == key(b"same")  # httpx draws a new boundary per request
    assert key(b"same") != key(b"other")
    assert key(b"same") != key(b"same", name="other.png")


# ---- FixtureTransport ------------------------------------------------------------------------------------


def test_replays_a_recording_matched_on_method_host_path_and_body(tmp_path):
    write_recording(
        tmp_path,
        "gen",
        method="POST",
        url="https://api.example.com/v1/gen",
        body={"prompt": "p", "n": 1},
        response={"status": 200, "json": {"id": "abc"}},
    )
    transport = FixtureTransport(tmp_path)
    with client_for(transport) as c:
        r = c.post("https://api.example.com/v1/gen", content=b'{ "n": 1,   "prompt": "p" }')
        assert (r.status_code, r.json()) == (200, {"id": "abc"})
        r = c.post(
            "https://API.example.com/v1/gen", json={"n": 1, "prompt": "p"}
        )  # host case does not matter
        assert r.json() == {"id": "abc"}
    assert [(s.method, s.host, s.path, s.recording) for s in transport.served] == [
        ("POST", "api.example.com", "/v1/gen", "gen.json")
    ] * 2


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "https://api.example.com/v1/gen", {"prompt": "different"}),  # same path, other body
        ("GET", "https://api.example.com/v1/gen", None),  # other method
        ("POST", "https://api.example.com/v1/other", {"prompt": "p", "n": 1}),  # other path
        ("POST", "https://evil.example/v1/gen", {"prompt": "p", "n": 1}),  # other host
    ],
)
def test_unrecorded_requests_raise_and_never_fall_through(tmp_path, method, url, body):
    write_recording(
        tmp_path, "gen", method="POST", url="https://api.example.com/v1/gen", body={"prompt": "p", "n": 1}
    )
    transport = FixtureTransport(tmp_path)
    with client_for(transport) as c, pytest.raises(UnrecordedRequest, match="no recording for"):
        c.request(method, url, json=body)
    assert transport.served == []
    assert isinstance(UnrecordedRequest("x"), RuntimeError)


def test_unrecorded_message_lists_the_recorded_bodies_for_that_path(tmp_path):
    write_recording(tmp_path, "gen", method="POST", url="https://h.example/p", body={"a": 1})
    transport = FixtureTransport(tmp_path)
    with client_for(transport) as c, pytest.raises(UnrecordedRequest) as exc:
        c.post("https://h.example/p", json={"a": 2})
    assert json_body_key({"a": 1}) in str(exc.value)
    assert "gen.json" in str(exc.value)
    assert json_body_key({"a": 2}) in str(exc.value)


def test_an_empty_directory_answers_nothing(tmp_path):
    transport = FixtureTransport(tmp_path)
    assert transport.recordings == []
    with client_for(transport) as c, pytest.raises(UnrecordedRequest):
        c.get("https://api.atlascloud.ai/api/v1/model/prediction/x")


def test_the_socket_block_is_really_on():
    """Belt and braces: a plain httpx client with no mock cannot connect from this folder."""
    with pytest.raises(Exception, match="network disabled"):
        httpx.get("https://api.atlascloud.ai/")


def test_query_strings(tmp_path):
    write_recording(
        tmp_path,
        "any",
        method="GET",
        url="https://h.example/get_result",
        response={"status": 200, "json": {"v": "any"}},
    )
    write_recording(
        tmp_path,
        "one",
        method="GET",
        url="https://h.example/get_result?id=job-1",
        response={"status": 200, "json": {"v": "one"}},
    )
    with client_for(FixtureTransport(tmp_path)) as c:
        assert c.get("https://h.example/get_result?id=job-1").json() == {"v": "one"}  # the specific one wins
        assert c.get("https://h.example/get_result?id=job-2").json() == {"v": "any"}
        assert c.get("https://h.example/get_result").json() == {"v": "any"}


def test_a_query_specific_recording_does_not_match_other_queries(tmp_path):
    write_recording(tmp_path, "one", method="GET", url="https://h.example/r?id=1", response={"status": 200})
    with client_for(FixtureTransport(tmp_path)) as c:
        assert c.get("https://h.example/r?id=1").status_code == 200
        with pytest.raises(UnrecordedRequest):
            c.get("https://h.example/r?id=2")
        with pytest.raises(UnrecordedRequest):
            c.get("https://h.example/r")


def test_responses_are_served_in_order_and_the_last_one_repeats(tmp_path):
    write_recording(
        tmp_path,
        "poll",
        method="GET",
        url="https://h.example/job",
        responses=[
            {"status": 200, "json": {"s": "running"}},
            {"status": 200, "json": {"s": "done"}},
        ],
    )
    with client_for(FixtureTransport(tmp_path)) as c:
        got = [c.get("https://h.example/job").json()["s"] for _ in range(4)]
    assert got == ["running", "done", "done", "done"]


def test_binary_bodies_headers_and_error_statuses(tmp_path):
    write_recording(
        tmp_path,
        "img",
        method="GET",
        url="https://h.example/out.png",
        response={
            "status": 200,
            "bytes_b64": base64.b64encode(b"\x89PNG\r\n").decode(),
            "headers": {"X-Seen": "1"},
        },
    )
    write_recording(tmp_path, "boom", method="GET", url="https://h.example/boom", response={"status": 503})
    with client_for(FixtureTransport(tmp_path)) as c:
        r = c.get("https://h.example/out.png")
        assert (r.content, r.headers["x-seen"]) == (b"\x89PNG\r\n", "1")
        assert c.get("https://h.example/boom").status_code == 503


def test_the_served_log_keeps_the_body_for_assertions(tmp_path):
    write_recording(tmp_path, "gen", method="POST", url="https://h.example/g", body={"a": 1})
    transport = FixtureTransport(tmp_path)
    with client_for(transport) as c:
        c.post("https://h.example/g", json={"a": 1}, headers={"Authorization": "Bearer k"})
    (call,) = transport.served
    assert json.loads(call.body) == {"a": 1}
    assert call.headers["authorization"] == "Bearer k"


def test_nested_directories_load_and_the_shipped_contract_dir_loads():
    assert CONTRACT_DIR.is_dir()
    shipped = load_recordings(CONTRACT_DIR)
    assert {r.name.split("/")[0] for r in shipped} >= {"atlas"}


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ("[]", "object with a 'request'"),
        ('{"request": {"method": "GET"}, "response": {"status": 200}}', "request.url is required"),
        ('{"request": {"url": "/relative"}, "response": {"status": 200}}', "has no host"),
        (
            '{"request": {"url": "https://h/x", "body": {}, "body_hash": "abcd1234"}, "response": {"status": 200}}',
            "not both",
        ),
        (
            '{"request": {"url": "https://h/x", "body_hash": "XYZ"}, "response": {"status": 200}}',
            "8 lower-case hex",
        ),
        ('{"request": {"url": "https://h/x"}}', "exactly one of"),
        (
            '{"request": {"url": "https://h/x"}, "response": {"status": 200}, "responses": []}',
            "exactly one of",
        ),
        ('{"request": {"url": "https://h/x"}, "responses": []}', "is empty"),
        ('{"request": {"url": "https://h/x"}, "response": {"json": {}}}', "integer status"),
        (
            '{"request": {"url": "https://h/x"}, "response": {"status": 200, "json": {}, "bytes_b64": ""}}',
            "not both",
        ),
        ("{not json", "unreadable recording bad.json"),
    ],
)
def test_malformed_recordings_fail_loudly_naming_the_file(tmp_path, doc, needle):
    (tmp_path / "bad.json").write_text(doc, encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match=needle):
        FixtureTransport(tmp_path)


def test_two_recordings_for_the_same_request_are_ambiguous(tmp_path):
    write_recording(tmp_path, "a", method="GET", url="https://h.example/x", response={"status": 200})
    write_recording(tmp_path, "b", method="GET", url="https://h.example/x", response={"status": 201})
    with pytest.raises(ValueError, match="duplicate recording for GET h.example/x"):
        FixtureTransport(tmp_path)


def test_write_recording_is_utf8_lf_and_refuses_unsafe_names(tmp_path):
    path = write_recording(
        tmp_path,
        "umlaut",
        method="get",
        url="https://h.example/x",
        response={"status": 200, "json": {"s": "Grüße"}},
    )
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert "Grüße".encode() in raw
    assert json.loads(raw.decode("utf-8"))["request"]["method"] == "GET"
    for bad in ("a:b", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="plain file stem"):
            write_recording(tmp_path, bad, method="GET", url="https://h.example/x")


# ---- LiveTransport and AllowlistTransport ----------------------------------------------------------------


def test_live_transport_refuses_to_exist_without_plate_live(monkeypatch):
    with pytest.raises(LiveDisabled, match="PLATE_LIVE=1"):
        LiveTransport()
    monkeypatch.setenv("PLATE_LIVE", "0")
    with pytest.raises(LiveDisabled):
        LiveTransport()
    monkeypatch.setenv("PLATE_LIVE", "true")
    with pytest.raises(LiveDisabled):
        LiveTransport()


def test_live_transport_can_be_constructed_only_when_plate_live_is_1(monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    transport = LiveTransport()  # construction opens no socket
    assert isinstance(transport, httpx.HTTPTransport)
    transport.close()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://api.openai.com/v1/images/edits", True),
        ("https://x.oss-ap-southeast-1.aliyuncs.com/a.png", True),
        ("https://img.atlascloud.ai/o.png", True),
        ("https://API.ATLASCLOUD.AI/o.png", True),
        ("https://aliyuncs.com/a.png", False),  # bare domain: the entries start with a dot
        ("https://aliyuncs.com.evil.example/a.png", False),
        ("https://evilatlascloud.ai/a.png", False),
        ("https://api.openai.com.evil.example/", False),
        ("https://evil.example/?u=api.openai.com", False),
        ("https://sub.api.openai.com/", False),  # an exact entry is exact
    ],
)
def test_allowlist_transport(tmp_path, url, allowed):
    write_recording(tmp_path, "any", method="GET", url=url, response={"status": 200})
    inner = FixtureTransport(tmp_path)
    guarded = AllowlistTransport(inner, ["api.openai.com", ".aliyuncs.com", ".atlascloud.ai"])
    with client_for(guarded) as c:
        if allowed:
            assert c.get(url).status_code == 200
        else:
            with pytest.raises(HostNotAllowed):
                c.get(url)
            assert inner.served == []  # blocked before the inner transport saw it


def test_an_empty_allowlist_blocks_everything(tmp_path):
    write_recording(tmp_path, "any", method="GET", url="https://h.example/x", response={"status": 200})
    with client_for(AllowlistTransport(FixtureTransport(tmp_path), [])) as c, pytest.raises(HostNotAllowed):
        c.get("https://h.example/x")


# ---- Atlas client over fixtures (shipped recordings) -----------------------------------------------------


def atlas_client(clock: Clock, transport: httpx.BaseTransport | None = None) -> AtlasClient:
    guarded = AllowlistTransport(
        transport or FixtureTransport(CONTRACT_DIR / "atlas"), [".atlascloud.ai", ".aliyuncs.com"]
    )
    return AtlasClient(HttpxAtlasTransport(guarded), sleep=clock.sleep, now=clock.now)


def test_atlas_image_flow_end_to_end_on_recordings(tmp_path):
    clock = Clock()
    client = atlas_client(clock)
    plate = tmp_path / "plate.png"
    plate.write_bytes(b"PNG-fixture-plate")
    url = client.upload(plate, mime="image/png")
    assert url == "https://img.atlascloud.ai/in/plate.png"
    pid = client.generate_image(
        {"model": "fixture/image-edit", "prompt": "relight to dusk", "images": [url], "resolution": "2K"}
    )
    assert pid == "pred-img-1"
    out = client.poll_image(pid)
    assert out == "https://img.atlascloud.ai/out/o1.png"
    assert clock.sleeps == [3.0, 3.0]  # processing, then completed
    dest = client.download(out, tmp_path / "out" / "o1.png")
    assert dest.read_bytes() == b"RESULT-IMAGE-BYTES"


def test_atlas_upload_of_a_different_file_is_unrecorded(tmp_path):
    other = tmp_path / "plate.png"
    other.write_bytes(b"some other plate")
    with pytest.raises(UnrecordedRequest):
        atlas_client(Clock()).upload(other, mime="image/png")


def test_atlas_video_flow_end_to_end_on_recordings():
    clock = Clock()
    client = atlas_client(clock)
    pid = client.generate_video(
        {
            "model": "kwaivgi/kling-v3.0-pro/image-to-video",
            "image": "https://img.atlascloud.ai/in/plate.png",
            "image_tail": "https://img.atlascloud.ai/in/end.png",
            "prompt": "She walks slowly to the gate.",  # 'slowly' is stripped before the body is keyed
            "resolution": "1080p",
            "duration": 5.0,
            "ratio": "16:9",
        }
    )
    assert pid == "pred-vid-1"
    assert client.poll_video(pid) == "https://cdn.atlascloud.ai/out/clip.mp4"
    assert clock.sleeps == [5.0, 5.0, 5.0]


def test_atlas_moderated_prediction_is_refused_on_recordings():
    with pytest.raises(AtlasRefused):
        atlas_client(Clock()).poll("pred-vid-refused")


def test_atlas_poll_times_out_on_a_recording_that_never_finishes(tmp_path):
    write_recording(
        tmp_path,
        "stuck",
        method="GET",
        url=f"{ATLAS}/model/prediction/stuck",
        response={"status": 200, "json": {"data": {"status": "processing"}}},
    )
    clock = Clock()
    started = time.perf_counter()
    with pytest.raises(AtlasTimeout):
        atlas_client(clock, FixtureTransport(tmp_path)).poll_video("stuck")
    assert time.perf_counter() - started < 5.0 and len(clock.sleeps) == 144


def test_httpx_atlas_transport_maps_non_json_bodies_to_none(tmp_path):
    write_recording(
        tmp_path,
        "html",
        method="GET",
        url="https://h.example/x",
        response={"status": 502, "bytes_b64": base64.b64encode(b"<html>").decode()},
    )
    write_recording(
        tmp_path, "list", method="GET", url="https://h.example/y", response={"status": 200, "json": [1, 2]}
    )
    t = HttpxAtlasTransport(FixtureTransport(tmp_path))
    r = t.request("GET", "https://h.example/x")
    assert (r.status, r.json, r.body) == (502, None, b"<html>")
    assert t.request("GET", "https://h.example/y").json is None  # a JSON array is not an object body
    t.close()


def test_atlas_client_is_re_exported_by_the_adapter_base():
    assert AtlasClient is atlas_module.AtlasClient
    assert base.AtlasClient is atlas_module.AtlasClient


# ---- Adapter base ----------------------------------------------------------------------------------------


class Probe(Adapter):
    """The smallest concrete adapter: quote from the manifest, canned submit and poll."""

    def quote(self, inputs, params):
        self.check_inputs(inputs)
        params = self.validate_params(params)
        return self.make_quote(
            self.estimate(tier=params.get("resolution"), seconds=params.get("duration", 5)),
            30.0,
            EngineSize(w=0, h=0, label="auto"),
        )

    def submit(self, staged: Staged) -> str:
        return "job-1"

    def poll(self, provider_job_id: str) -> JobStatus:
        return JobStatus(state="done", outputs=["https://img.atlascloud.ai/out/o1.png"])


@pytest.fixture
def recorded_dir(tmp_path):
    write_recording(
        tmp_path,
        "o1",
        method="GET",
        url="https://img.atlascloud.ai/out/o1.png",
        response={"status": 200, "bytes_b64": base64.b64encode(b"IMG").decode()},
    )
    write_recording(
        tmp_path, "gone", method="GET", url="https://img.atlascloud.ai/out/gone.png", response={"status": 404}
    )
    return tmp_path


def test_a_cloud_adapter_cannot_be_built_without_a_transport(manifests):
    with pytest.raises(MissingTransport, match="pass a transport"):
        Probe(manifests["kling-o3-4k"])
    with pytest.raises(MissingTransport):
        Probe(manifests["gpt-image-2.5"], transport=None)


def test_a_local_adapter_needs_no_transport_and_makes_no_http_calls(manifests):
    adapter = Probe(manifests["qwen-image-edit-2509"])
    assert adapter.custody_grade == "C0" and adapter.route_id == "local"
    with pytest.raises(MissingTransport, match="no HTTP calls"):
        adapter.http()
    with pytest.raises(MissingTransport):
        adapter.fetch("https://img.atlascloud.ai/o.png")


def test_route_selection(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    m = manifests["nano-banana-2.1"]
    assert Probe(m, transport=t).route_id == "gemini"  # first active route
    assert Probe(m, "atlas", t).route_id == "atlas"
    assert Probe(m, m.routes[1], t).route_id == "atlas"
    with pytest.raises(ValueError, match="no route 'nope'"):
        Probe(m, "nope", t)
    with pytest.raises(ValueError, match="not a route of"):
        Probe(m, manifests["veo-3.1"].routes[0], t)
    retired = m.model_copy(
        update={"routes": [m.routes[0].model_copy(update={"status": "retired"}), m.routes[1]]}
    )
    assert Probe(retired, transport=t).route_id == "atlas"  # retired routes are skipped by default
    with pytest.raises(ValueError, match="retired"):
        Probe(retired, "gemini", t)
    only_retired = m.model_copy(update={"routes": [m.routes[0].model_copy(update={"status": "retired"})]})
    with pytest.raises(ValueError, match="no active route"):
        Probe(only_retired, transport=t)


def test_identity_and_effective_custody(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    gem = Probe(manifests["nano-banana-pro"], transport=t)
    assert gem.model_version == "gemini-3-pro-image"
    assert gem.custody_grade == "D"  # pending written terms counts as D
    kling = Probe(manifests["kling-o3-4k"], transport=t)
    assert kling.model_version == "kwaivgi/kling-video-o3-4k/image-to-video"  # the route slug
    assert Probe(manifests["nano-banana-2.1"], transport=t).model_version == "unpinned"  # no model id yet
    assert Probe(manifests["real-esrgan"]).model_version == "RealESRGAN_x4plus"


def test_validate_params_exposes_the_full_vendor_schema(manifests, recorded_dir):
    a = Probe(manifests["seedance-2.5-i2v"], transport=FixtureTransport(recorded_dir))
    ok = a.validate_params(
        {
            "duration": 8,
            "resolution": "720P",
            "ratio": "16:9",
            "camera_fixed": True,
            "seed": 7,
            "return_last_frame": True,
        }
    )
    assert ok["duration"] == 8 and ok["return_last_frame"] is True
    assert ok["generate_audio"] is False and ok["resolution"] == "720P"
    defaults = a.validate_params({})
    assert defaults == {
        "duration": 5,
        "resolution": "480P",
        "ratio": "adaptive",
        "camera_fixed": False,
        "generate_audio": False,
        "return_last_frame": False,
    }


@pytest.mark.parametrize(
    ("engine", "params", "needle"),
    [
        ("seedance-2.5-i2v", {"durationn": 5}, "unknown parameter"),
        ("seedance-2.5-i2v", {"image": "https://x"}, "unknown parameter"),  # inputs are not params
        ("seedance-2.5-i2v", {"duration": 3}, "outside 4 to 30"),
        ("seedance-2.5-i2v", {"duration": 31}, "outside 4 to 30"),
        ("seedance-2.5-i2v", {"duration": 4.5}, "expected an integer"),
        ("seedance-2.5-i2v", {"duration": "5"}, "expected a number"),
        ("seedance-2.5-i2v", {"duration": True}, "expected a number"),
        ("seedance-2.5-i2v", {"resolution": "4K"}, "not one of"),  # Seedance 2.5 has no 4K
        ("seedance-2.5-i2v", {"resolution": "1080p"}, "not one of"),
        ("seedance-2.5-i2v", {"camera_fixed": "yes"}, "expected true or false"),
        ("kling-o3-4k", {"duration": 16}, "outside 1 to 15"),
        ("kling-o3-4k", {"negative_prompt": 3}, "expected a string"),
        ("gpt-image-2.5", {"size": "4096x4096"}, "not one of"),
        ("gpt-image-2.5", {"seed": 1}, "unknown parameter"),  # OpenAI Images has no seed
    ],
)
def test_validate_params_rejects(manifests, recorded_dir, engine, params, needle):
    a = Probe(manifests[engine], transport=FixtureTransport(recorded_dir))
    with pytest.raises(ParamError, match=needle):
        a.validate_params(params)


def test_validate_params_integral_floats_none_and_float_params(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    a = Probe(manifests["seedance-2.5-i2v"], transport=t)
    out = a.validate_params({"duration": 6.0, "seed": None})
    assert out["duration"] == 6 and type(out["duration"]) is int and "seed" not in out
    q = Probe(manifests["qwen-image-edit-2509"])
    assert q.validate_params({"true_cfg_scale": 3})["true_cfg_scale"] == 3.0
    with pytest.raises(ParamError, match="outside 1 to 20"):
        q.validate_params({"true_cfg_scale": 0.5})
    assert q.validate_params({"negative_prompt": None, "num_inference_steps": 40}) == {
        "num_inference_steps": 40,
        "true_cfg_scale": 4.0,
        "controlnet_depth_scale": 1.0,
        "controlnet_edge_scale": 1.0,
        "precision": "fp8",
    }


def test_check_inputs(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    r2v = Probe(manifests["seedance-2.5-r2v"], transport=t)
    r2v.check_inputs({"reference_images": ["a"] * 30, "prompt": "p"})
    with pytest.raises(InputError, match="30"):
        r2v.check_inputs({"reference_images": ["a"] * 31, "prompt": "p"})
    with pytest.raises(InputError, match="reference_videos has 11 items, the cap is 10"):
        r2v.check_inputs({"reference_images": ["a"], "reference_videos": ["v"] * 11, "prompt": "p"})
    with pytest.raises(InputError, match=r"missing required input\(s\) \['prompt'\]"):
        r2v.check_inputs({"reference_images": ["a"]})
    with pytest.raises(InputError, match="missing required"):
        r2v.check_inputs({"reference_images": ["a"], "prompt": None})
    with pytest.raises(InputError, match=r"unknown input\(s\) \['image'\]"):
        r2v.check_inputs({"image": "x", "reference_images": ["a"], "prompt": "p"})


def test_the_plate_never_reaches_an_upscaler(manifests):
    esrgan = Probe(manifests["real-esrgan"])
    esrgan.check_inputs({"image": {"path": "layer.png", "role": "ai_layer"}})
    with pytest.raises(PlateUpscaleForbidden, match="plate is never upscaled"):
        esrgan.check_inputs({"image": {"path": "plate.png", "role": "plate"}})
    # a plain engine is not an upscaler and has no such rule
    Probe(manifests["qwen-image-edit-2509"]).check_inputs(
        {"image": {"path": "p.png", "role": "plate"}, "prompt": "p"}
    )
    assert issubclass(PlateUpscaleForbidden, InputError)


def test_estimate_uses_tier_prices_then_the_default_unit_price(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    kling = Probe(manifests["kling-o3-4k"], transport=t)
    assert kling.estimate(seconds=5) == pytest.approx(0.357 * 5)
    assert kling.estimate(tier="atlas_discounted", seconds=5) == pytest.approx(0.0536 * 5)
    assert kling.estimate(tier="unlisted", seconds=2) == pytest.approx(
        0.357 * 2
    )  # unpriced tier: the default unit
    seedream = Probe(manifests["seedream-5-pro"], transport=t)
    assert seedream.estimate(tier="2K", images=3) == pytest.approx(0.23 * 3)
    with pytest.raises(ValueError, match="needs seconds"):
        kling.estimate()


def test_every_priced_cloud_engine_can_be_estimated(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    for m in manifests.values():
        a = Probe(m, transport=t)
        got = a.estimate(seconds=5, megapixels=2.0)
        assert got >= 0
        if a.route.provider != "local":
            assert got > 0, m.id


def test_quote_shape_and_honesty(manifests, recorded_dir):
    t = FixtureTransport(recorded_dir)
    q = Probe(manifests["kling-o3-4k"], transport=t).quote(
        {"image": "s.png", "prompt": "p"}, {"duration": 4, "resolution": "1080P"}
    )
    assert isinstance(q, Quote)
    assert (q.engine_id, q.route_id, q.custody_grade, q.price_status) == (
        "kling-o3-4k",
        "atlas",
        "D",
        "unconfirmed",
    )
    assert q.model_version == "kwaivgi/kling-video-o3-4k/image-to-video"
    assert q.price_usd == pytest.approx(0.357 * 4) and q.eta_s == 30.0
    assert q.size == EngineSize(w=0, h=0, label="auto")
    local = Probe(manifests["real-esrgan"]).quote({"image": "a.png"}, {})
    assert (local.custody_grade, local.price_status, local.price_usd) == ("C0", "confirmed", 0.0)


def test_quote_and_job_status_models_are_strict_and_frozen():
    with pytest.raises(ValueError):
        Quote(
            engine_id="e",
            route_id="r",
            price_usd=-1,
            eta_s=0,
            custody_grade="D",
            size=EngineSize(w=1, h=1),
            model_version="v",
        )
    with pytest.raises(ValueError):
        JobStatus(state="exploded")
    with pytest.raises(ValueError):
        JobStatus(state="done", surprise=1)
    s = JobStatus(state="done", outputs=["u"], output_fps=24.0, output_bits=10, usage={"tokens": 5})
    with pytest.raises(ValueError):
        s.state = "failed"
    assert Staged(inputs={"a": 1}, params={}).size is None


def test_fetch_is_allowlisted_and_sends_no_credentials(manifests, recorded_dir):
    transport = FixtureTransport(recorded_dir)
    a = Probe(manifests["kling-o3-4k"], transport=transport)
    assert a.fetch("https://img.atlascloud.ai/out/o1.png") == b"IMG"
    assert "authorization" not in transport.served[0].headers
    with pytest.raises(FetchError, match="download failed: 404"):
        a.fetch("https://img.atlascloud.ai/out/gone.png")
    with pytest.raises(HostNotAllowed):
        a.fetch("https://evil.example/o1.png")
    assert len(transport.served) == 2  # the evil host never reached the transport
    a.close()


def test_fetch_on_a_route_with_no_allowed_hosts_fails_closed(manifests, recorded_dir):
    m = manifests["kling-o3-4k"]
    bare = m.model_copy(update={"routes": [m.routes[0].model_copy(update={"allowed_hosts": []})]})
    a = Probe(bare, transport=FixtureTransport(recorded_dir))
    with pytest.raises(HostNotAllowed):
        a.fetch("https://img.atlascloud.ai/out/o1.png")


def test_adapter_atlas_client_rides_the_guarded_transport(manifests):
    clock = Clock()
    a = Probe(manifests["kling-o3-4k"], transport=FixtureTransport(CONTRACT_DIR / "atlas"))
    client = a.atlas_client(sleep=clock.sleep, now=clock.now)
    assert client.poll("pred-img-1") == "https://img.atlascloud.ai/out/o1.png"
    # the same transport refuses a route it does not list
    gemini = Probe(manifests["nano-banana-pro"], transport=FixtureTransport(CONTRACT_DIR / "atlas"))
    with pytest.raises(HostNotAllowed):
        gemini.atlas_client(sleep=clock.sleep, now=clock.now).poll("pred-img-1")


def test_the_adapter_interface_is_abstract(manifests):
    with pytest.raises(TypeError, match="abstract"):
        Adapter(manifests["real-esrgan"])  # type: ignore[abstract]


def test_recordings_path_is_inside_the_package():
    assert Path(CONTRACT_DIR).name == "contract" and CONTRACT_DIR.parent.name == "plate_engines"
