"""Contract tests for the Atlas client: one test per quirk of ~/Desktop/staffage/src/lib/atlas.ts.

Everything runs against FakeTransport with a recording sleep and a fake clock, so nothing touches the network
and nothing really sleeps. Line numbers in the docstrings refer to atlas.ts.
"""

from __future__ import annotations

import time

import pytest

from plate_engines.atlas import (
    ATLAS_BASE,
    IMAGE_POLL_INTERVAL_S,
    IMAGE_POLL_TIMEOUT_S,
    RESULT_HOSTS,
    VIDEO_POLL_INTERVAL_S,
    VIDEO_POLL_TIMEOUT_S,
    AtlasClient,
    AtlasError,
    AtlasInputError,
    AtlasPrediction,
    AtlasRefused,
    AtlasTerminal,
    AtlasTimeout,
    FakeTransport,
    TransportResponse,
    atlas_duration,
    atlas_ratio,
    atlas_resolution,
    classify_prediction,
    strip_pace_words,
    unwrap,
)

KEY = "test-key-123"
IMG = {"images": ["https://x.aliyuncs.com/a.png"], "prompt": "p"}


class Clock:
    """Fake monotonic clock: sleep() records the request and advances time instead of waiting."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.events: list[str] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.events.append("sleep")
        self.t += seconds

    def now(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ATLASCLOUD_KEY", KEY)


def make(script, clock):
    transport = FakeTransport(script)
    return AtlasClient(transport, sleep=clock.sleep, now=clock.now), transport


def ok(payload, status=200):
    return TransportResponse(status=status, json=payload)


# ---- constants and key (atlas.ts:8-15) -------------------------------------------------------------


def test_constants():
    assert ATLAS_BASE == "https://api.atlascloud.ai/api/v1"
    assert RESULT_HOSTS == (".aliyuncs.com", ".atlascloud.ai")


def test_missing_key_raises(monkeypatch, clock):
    monkeypatch.delenv("ATLASCLOUD_KEY")
    client, transport = make([], clock)
    with pytest.raises(AtlasError, match=r"^ATLASCLOUD_KEY missing — add it to the environment$"):
        client.key()
    with pytest.raises(AtlasError, match="ATLASCLOUD_KEY missing"):
        client.generate_image(IMG)
    assert transport.calls == []


def test_empty_key_counts_as_missing(monkeypatch, clock):
    monkeypatch.setenv("ATLASCLOUD_KEY", "")
    client, _ = make([], clock)
    with pytest.raises(AtlasError, match="ATLASCLOUD_KEY missing"):
        client.key()


def test_custom_key_env(monkeypatch, clock):
    monkeypatch.setenv("MY_ATLAS", "other")
    client = AtlasClient(FakeTransport([]), key_env="MY_ATLAS")
    assert client.key() == "other"


# ---- upload (atlas.ts:17-30) -----------------------------------------------------------------------


def test_upload_request_shape(tmp_path, clock):
    f = tmp_path / "plate.png"
    f.write_bytes(b"\x89PNGdata")
    client, transport = make([ok({"data": {"download_url": "https://x.aliyuncs.com/a.png"}})], clock)
    url = client.upload(f, mime="image/png")
    assert url == "https://x.aliyuncs.com/a.png"
    (call,) = transport.calls
    assert call.method == "POST"
    assert call.url == f"{ATLAS_BASE}/model/uploadMedia"
    assert call.headers == {
        "Authorization": f"Bearer {KEY}"
    }  # no Content-Type: the transport sets the boundary
    assert call.files == {"file": ("plate.png", b"\x89PNGdata", "image/png")}
    assert call.json is None


def test_upload_default_mime(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, transport = make([ok({"download_url": "https://cdn.atlascloud.ai/a"})], clock)
    client.upload(f)
    assert transport.calls[0].files["file"][2] == "application/octet-stream"


def test_upload_reads_nested_url_before_top_level(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    payload = {"data": {"download_url": "https://nested/x"}, "download_url": "https://top/x"}
    client, _ = make([ok(payload)], clock)
    assert client.upload(f) == "https://nested/x"


def test_upload_falls_back_to_top_level_url(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, _ = make([ok({"download_url": "https://top/x"})], clock)
    assert client.upload(f) == "https://top/x"


def test_upload_failure_message_truncates_body_to_300(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, _ = make([ok({"error": "x" * 500}, status=500)], clock)
    with pytest.raises(AtlasError) as exc:
        client.upload(f)
    msg = str(exc.value)
    assert msg.startswith('uploadMedia failed: 500 {"error":"xxx')
    assert len(msg) == len("uploadMedia failed: 500 ") + 300


def test_upload_http_ok_without_url_still_fails(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, _ = make([ok({"data": {}})], clock)
    with pytest.raises(AtlasError, match=r'^uploadMedia failed: 200 \{"data":\{\}\}$'):
        client.upload(f)


def test_upload_url_on_an_error_status_is_still_a_failure(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, _ = make([ok({"download_url": "https://top/x"}, status=502)], clock)
    with pytest.raises(AtlasError, match="uploadMedia failed: 502"):
        client.upload(f)


def test_upload_non_json_body_is_treated_as_empty_object(tmp_path, clock):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1")
    client, _ = make([TransportResponse(status=502, json=None, body=b"<html>bad gateway</html>")], clock)
    with pytest.raises(AtlasError, match=r"^uploadMedia failed: 502 \{\}$"):
        client.upload(f)


# ---- generateImage (atlas.ts:32-42) ----------------------------------------------------------------


def test_generate_image_request_shape(clock):
    client, transport = make([ok({"data": {"id": "pred-1"}})], clock)
    body = {"model": "demo", "prompt": "p", "images": ["https://x.aliyuncs.com/a.png"]}
    assert client.generate_image(body) == "pred-1"
    (call,) = transport.calls
    assert (call.method, call.url) == ("POST", f"{ATLAS_BASE}/model/generateImage")
    assert call.headers == {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    assert call.json == body


def test_generate_image_id_locations_and_stringification(clock):
    client, _ = make(
        [ok({"id": "top"}), ok({"data": {"id": 12345}}), ok({"data": {"id": "n"}, "id": "t"})], clock
    )
    assert client.generate_image(IMG) == "top"
    assert client.generate_image(IMG) == "12345"
    assert client.generate_image(IMG) == "n"


def test_generate_image_failures(clock):
    client, _ = make(
        [ok({"message": "bad"}, status=400), ok({"data": {}}), ok({"id": "x"}, status=500)], clock
    )
    with pytest.raises(AtlasError, match=r'^generateImage failed: 400 \{"message":"bad"\}$'):
        client.generate_image(IMG)
    with pytest.raises(AtlasError, match="^generateImage failed: 200"):
        client.generate_image(IMG)
    with pytest.raises(AtlasError, match="^generateImage failed: 500"):
        client.generate_image(IMG)


# ---- poll (atlas.ts:44-61) -------------------------------------------------------------------------


def running():
    return ok({"data": {"status": "processing", "outputs": []}})


def test_poll_sleeps_before_the_first_request_and_returns_the_output(clock):
    def script(call):
        clock.events.append("get")
        return ok({"data": {"status": "completed", "outputs": ["https://cdn.atlascloud.ai/out.png"]}})

    client, transport = make(script, clock)
    started = time.perf_counter()
    assert client.poll("pred-1") == "https://cdn.atlascloud.ai/out.png"
    assert time.perf_counter() - started < 1.0  # the 3 s interval was recorded, not slept
    assert clock.sleeps == [3.0]
    assert clock.events == ["sleep", "get"]
    (call,) = transport.calls
    assert (call.method, call.url) == ("GET", f"{ATLAS_BASE}/model/prediction/pred-1")
    assert call.headers == {"Authorization": f"Bearer {KEY}"}
    assert call.json is None


def test_poll_keeps_polling_until_an_output_appears(clock):
    client, transport = make([running(), running(), ok({"data": {"outputs": ["http://a/b.png"]}})], clock)
    assert client.poll("p") == "http://a/b.png"
    assert clock.sleeps == [3.0, 3.0, 3.0]
    assert len(transport.calls) == 3


def test_poll_accepts_a_payload_without_the_data_wrapper(clock):
    client, _ = make([ok({"status": "ok", "outputs": ["https://top.level/out"]})], clock)
    assert client.poll("p") == "https://top.level/out"


def test_poll_null_data_falls_back_to_the_top_level(clock):
    client, _ = make([ok({"data": None, "outputs": ["https://top.level/out"]})], clock)
    assert client.poll("p") == "https://top.level/out"


def test_poll_ignores_outputs_that_are_not_http_strings(clock):
    script = [
        ok({"data": {"outputs": ["not-a-url"]}}),
        ok({"data": {"outputs": [{"url": "https://x"}]}}),
        ok({"data": {"outputs": "https://not-a-list"}}),
        ok({"data": {"outputs": ["https://ok/x"]}}),
    ]
    client, _ = make(script, clock)
    assert client.poll("p") == "https://ok/x"
    assert len(clock.sleeps) == 4


@pytest.mark.parametrize("status", ["failed", "FAILED", "Error", "error_timeout", "cancelled", "canceled"])
def test_poll_terminal_statuses(clock, status):
    client, _ = make([ok({"data": {"status": status, "reason": "boom"}})], clock)
    with pytest.raises(AtlasTerminal) as exc:
        client.poll("p")
    assert str(exc.value).startswith(f"generation {status.lower()}: ")
    assert '"reason":"boom"' in str(exc.value)
    assert isinstance(exc.value, AtlasError)


def test_poll_terminal_message_is_truncated_to_300_chars(clock):
    client, _ = make([ok({"data": {"status": "failed", "detail": "y" * 800}})], clock)
    with pytest.raises(AtlasTerminal) as exc:
        client.poll("p")
    assert len(str(exc.value)) == len("generation failed: ") + 300


def test_poll_output_wins_over_a_failure_status(clock):
    client, _ = make([ok({"data": {"status": "failed", "outputs": ["https://x/out"]}})], clock)
    assert client.poll("p") == "https://x/out"


def test_poll_does_not_check_the_http_status_or_body_type(clock):
    script = [
        TransportResponse(status=502, json=None, body=b"<html>"),  # unparsable: keep polling
        ok({"error": "not found"}, status=404),  # JSON error, no status field: keep polling
        ok({"data": {"outputs": ["https://x/out"]}}, status=200),
    ]
    client, transport = make(script, clock)
    assert client.poll("p") == "https://x/out"
    assert len(transport.calls) == 3


def test_poll_times_out_with_a_recorded_clock(clock):
    client, transport = make(lambda call: running(), clock)
    started = time.perf_counter()
    with pytest.raises(AtlasTimeout, match=r"^prediction slow-1 timed out$"):
        client.poll("slow-1")
    assert time.perf_counter() - started < 1.0
    assert clock.sleeps == [3.0] * 40  # 120 s / 3 s
    assert len(transport.calls) == 40
    assert isinstance(AtlasTimeout("x"), AtlasError)


def test_poll_honours_timeout_and_interval(clock):
    client, transport = make(lambda call: running(), clock)
    with pytest.raises(AtlasTimeout):
        client.poll("p", timeout_s=10.0, interval_s=2.5)
    assert clock.sleeps == [2.5] * 4
    assert len(transport.calls) == 4


def test_poll_with_zero_timeout_never_requests(clock):
    client, transport = make([], clock)
    with pytest.raises(AtlasTimeout):
        client.poll("p", timeout_s=0.0)
    assert transport.calls == [] and clock.sleeps == []


def test_poll_fails_fast_without_a_key(monkeypatch, clock):
    monkeypatch.delenv("ATLASCLOUD_KEY")
    client, transport = make([], clock)
    with pytest.raises(AtlasError, match="ATLASCLOUD_KEY missing"):
        client.poll("p")
    assert clock.sleeps == [] and transport.calls == []


def test_poll_id_cannot_escape_the_prediction_path(clock):
    client, transport = make([ok({"data": {"outputs": ["https://x/out"]}})], clock)
    client.poll("a/../../admin?x=1")
    assert transport.calls[0].url == f"{ATLAS_BASE}/model/prediction/a%2F..%2F..%2Fadmin%3Fx%3D1"


# ---- download (atlas.ts:63-71) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://foo.oss-ap-southeast-1.aliyuncs.com/x.png",
        "https://cdn.atlascloud.ai/x.png",
        "http://cdn.atlascloud.ai/x.png",
        "https://CDN.AtlasCloud.AI/x.png",
    ],
)
def test_download_accepts_allowlisted_hosts(tmp_path, clock, url):
    client, transport = make([TransportResponse(status=200, body=b"IMG")], clock)
    dest = tmp_path / "out" / "nested" / "x.png"
    assert client.download(url, dest) == dest
    assert dest.read_bytes() == b"IMG"  # parents created
    assert [c.url for c in transport.calls] == [url]


@pytest.mark.parametrize(
    "url",
    [
        "https://aliyuncs.com.evil.example/x.png",
        "https://evil.example/x.png",
        "https://aliyuncs.com/x.png",  # bare domain: the allowlist suffixes start with a dot
        "https://atlascloud.ai/x.png",
        "https://evilatlascloud.ai/x.png",
        "https://cdn.atlascloud.ai.evil.example/x.png",
        "https://cdn.atlascloud.ai@evil.example/x.png",
        "https://evil.example/?u=cdn.atlascloud.ai",
        "not a url",
    ],
)
def test_download_rejects_other_hosts_before_any_request(tmp_path, clock, url):
    client, transport = make([TransportResponse(status=200, body=b"IMG")], clock)
    with pytest.raises(AtlasError, match=r"^result host .* not allowlisted$"):
        client.download(url, tmp_path / "x.png")
    assert transport.calls == []
    assert not (tmp_path / "x.png").exists()


def test_download_reports_the_rejected_host(tmp_path, clock):
    client, _ = make([], clock)
    with pytest.raises(AtlasError, match=r"^result host aliyuncs\.com\.evil\.example not allowlisted$"):
        client.download("https://aliyuncs.com.evil.example/x.png", tmp_path / "x.png")


def test_download_never_sends_the_api_key_to_the_result_host(tmp_path, clock):
    client, transport = make([TransportResponse(status=200, body=b"IMG")], clock)
    client.download("https://cdn.atlascloud.ai/x.png", tmp_path / "x.png")
    (call,) = transport.calls
    assert call.method == "GET"
    assert call.headers == {}


def test_download_works_without_a_key(monkeypatch, tmp_path, clock):
    monkeypatch.delenv("ATLASCLOUD_KEY")
    client, _ = make([TransportResponse(status=200, body=b"IMG")], clock)
    client.download("https://cdn.atlascloud.ai/x.png", tmp_path / "x.png")
    assert (tmp_path / "x.png").read_bytes() == b"IMG"


def test_download_failure_status(tmp_path, clock):
    client, _ = make([TransportResponse(status=403, body=b"nope")], clock)
    with pytest.raises(AtlasError, match=r"^download failed: 403$"):
        client.download("https://cdn.atlascloud.ai/x.png", tmp_path / "x.png")
    assert not (tmp_path / "x.png").exists()


def test_download_is_atomic_and_overwrites(tmp_path, clock):
    dest = tmp_path / "x.png"
    dest.write_bytes(b"old")
    client, _ = make([TransportResponse(status=200, body=b"new")], clock)
    client.download("https://cdn.atlascloud.ai/x.png", dest)
    assert dest.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["x.png"]  # no .part leftovers


# ---- FakeTransport itself --------------------------------------------------------------------------


def test_fake_transport_exhausted_script_fails_loudly():
    transport = FakeTransport([])
    with pytest.raises(AssertionError, match="exhausted"):
        transport.request("GET", "https://x")


def test_fake_transport_callable_script_and_recording():
    transport = FakeTransport(lambda call: ok({"echo": call.url}))
    response = transport.request("GET", "https://x/y", headers={"A": "b"})
    assert response.json == {"echo": "https://x/y"}
    assert transport.calls[0].headers == {"A": "b"}


# ---- T7 quirks: images array, video body rules, refusal mapping, cadence, pace words ----------------------


@pytest.mark.parametrize(
    "body",
    [
        {
            "image": "https://x.aliyuncs.com/a.png",
            "prompt": "p",
        },  # lone `image`: silently ignored by the vendor
        {"image": "https://x.aliyuncs.com/a.png", "images": ["https://x.aliyuncs.com/a.png"]},
        {"images": "https://x.aliyuncs.com/a.png", "prompt": "p"},  # a string is not an array
        {"images": [], "prompt": "p"},
        {"images": [""], "prompt": "p"},
        {"images": [None], "prompt": "p"},
        {"prompt": "text only"},  # text-to-image never leaves the client
        {},
    ],
)
def test_generate_image_rejects_a_body_without_an_images_array_before_sending(clock, body):
    client, transport = make([ok({"data": {"id": "never"}})], clock)
    with pytest.raises(AtlasInputError):
        client.generate_image(body)
    assert transport.calls == []
    assert isinstance(AtlasInputError("x"), AtlasError)


def test_generate_image_sends_the_images_array_untouched(clock):
    body = {"model": "m", "prompt": "p", "images": ["https://a/1.png", "https://a/2.png"], "resolution": "2K"}
    client, transport = make([ok({"data": {"id": "p1"}})], clock)
    client.generate_image(body)
    assert transport.calls[0].json == body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1080P", "1080P"),
        ("1080p", "1080P"),
        ("480p", "480P"),
        (720, "720P"),
        ("720", "720P"),
        (" 480 P ", "480P"),
    ],
)
def test_resolution_is_upper_case(raw, expected):
    assert atlas_resolution(raw) == expected


@pytest.mark.parametrize("raw", ["4k", "high", "", "1080x1920", True])
def test_resolution_rejects_what_it_cannot_normalise(raw):
    with pytest.raises(AtlasInputError):
        atlas_resolution(raw)


VIDEO = {"model": "kwaivgi/kling-v3.0-pro/image-to-video", "image": "https://a/1.png", "prompt": "a push-in"}


def test_generate_video_request_shape_and_normalised_body(clock):
    client, transport = make([ok({"data": {"id": "vid-1"}})], clock)
    pid = client.generate_video(
        {**VIDEO, "resolution": "1080p", "duration": 5.0, "ratio": "16:9", "seed": -1}
    )
    assert pid == "vid-1"
    (call,) = transport.calls
    assert (call.method, call.url) == ("POST", f"{ATLAS_BASE}/model/generateVideo")
    assert call.headers == {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    assert call.json["resolution"] == "1080P"
    assert call.json["duration"] == 5 and type(call.json["duration"]) is int
    assert call.json["ratio"] == "16:9" and call.json["seed"] == -1


def test_generate_video_does_not_mutate_the_callers_body(clock):
    body = {**VIDEO, "resolution": "720p", "duration": 4.0}
    snapshot = dict(body)
    client, _ = make([ok({"data": {"id": "v"}})], clock)
    client.generate_video(body)
    assert body == snapshot


@pytest.mark.parametrize("duration", [4.5, "5", None, True, [5]])
def test_generate_video_duration_must_be_an_integer(clock, duration):
    client, transport = make([ok({"data": {"id": "v"}})], clock)
    with pytest.raises(AtlasInputError, match="integer"):
        client.generate_video({**VIDEO, "duration": duration})
    assert transport.calls == []


def test_generate_video_id_locations_and_failures(clock):
    client, _ = make(
        [
            ok({"id": 77}),
            ok({"data": {"id": "n"}, "id": "t"}),
            ok({"message": "bad"}, status=400),
            ok({"data": {}}),
        ],
        clock,
    )
    assert client.generate_video(VIDEO) == "77"
    assert client.generate_video(VIDEO) == "n"
    with pytest.raises(AtlasError, match=r'^generateVideo failed: 400 \{"message":"bad"\}$'):
        client.generate_video(VIDEO)
    with pytest.raises(AtlasError, match="^generateVideo failed: 200"):
        client.generate_video(VIDEO)


def test_generate_video_needs_a_key_before_any_request(monkeypatch, clock):
    monkeypatch.delenv("ATLASCLOUD_KEY")
    client, transport = make([], clock)
    with pytest.raises(AtlasError, match="ATLASCLOUD_KEY missing"):
        client.generate_video(VIDEO)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("width", "height", "ratio"),
    [
        (1920, 1080, "16:9"),
        (1080, 1080, "16:9"),
        (1080, 1920, "9:16"),
        (640, 641, "9:16"),
        (3840, 1608, "16:9"),
    ],
)
def test_ratio_comes_from_the_source_dimensions(width, height, ratio):
    assert atlas_ratio(width, height) == ratio


@pytest.mark.parametrize(
    ("seconds", "rounding", "lo", "hi", "expected"),
    [
        (4.2, "ceil", 1, 15, 5),
        (4.0, "ceil", 1, 15, 4),
        (0.2, "ceil", 1, 15, 1),
        (99.0, "ceil", 1, 15, 15),
        (4.9, "floor", 1, 15, 4),
        (0.4, "floor", 1, 15, 1),
        (3.1, "ceil", 4, 30, 4),
        (31.0, "ceil", 4, 30, 30),
    ],
)
def test_duration_is_an_integer_clamped_to_the_models_limits(seconds, rounding, lo, hi, expected):
    got = atlas_duration(seconds, lo, hi, rounding)
    assert got == expected and type(got) is int


def test_unwrap_is_data_data_or_data():
    assert unwrap({"data": {"a": 1}, "b": 2}) == {"a": 1}
    assert unwrap({"a": 1}) == {"a": 1}
    assert unwrap({"data": None, "a": 1}) == {"data": None, "a": 1}
    assert unwrap({"data": "text"}) == {}
    assert unwrap({}) == {}


def test_success_is_outputs_zero_being_an_http_string():
    assert classify_prediction({"outputs": ["https://x/out", "https://x/two"]}) == AtlasPrediction(
        "done", ("https://x/out", "https://x/two"), status=""
    )
    for outputs in (["ftp://x"], [{"url": "https://x"}], "https://x", [], None):
        assert classify_prediction({"status": "processing", "outputs": outputs}).state == "running"
    assert classify_prediction({"status": "failed", "outputs": ["https://x/out"]}).state == "done"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("created", "queued"),
        ("Queued", "queued"),
        ("pending", "queued"),
        ("processing", "running"),
        ("running", "running"),
        ("", "running"),
        ("failed", "failed"),
        ("Error", "failed"),
        ("cancelled", "failed"),
    ],
)
def test_prediction_state_from_status(status, state):
    assert classify_prediction({"status": status}).state == state


@pytest.mark.parametrize(
    "message",
    [
        "content moderation rejected the image",
        "Safety filter triggered",
        "violates our POLICY",
        "NSFW content detected",
        "request was blocked",
    ],
)
def test_poll_maps_moderation_style_failures_to_refused(clock, message):
    client, _ = make([ok({"data": {"status": "failed", "message": message}})], clock)
    with pytest.raises(AtlasRefused) as exc:
        client.poll("p")
    assert str(exc.value).startswith("generation failed: ")
    assert isinstance(exc.value, AtlasTerminal)


def test_refusal_reads_the_error_object_too(clock):
    client, _ = make(
        [ok({"data": {"status": "error", "error": {"code": "E1", "message": "policy violation"}}})], clock
    )
    with pytest.raises(AtlasRefused):
        client.poll("p")


def test_ordinary_failures_are_terminal_but_not_refused(clock):
    client, _ = make([ok({"data": {"status": "failed", "message": "gpu out of memory"}})], clock)
    with pytest.raises(AtlasTerminal) as exc:
        client.poll("p")
    assert not isinstance(exc.value, AtlasRefused)


def test_an_echoed_prompt_cannot_turn_a_failure_into_a_refusal(clock):
    payload = {
        "status": "failed",
        "input": {"prompt": "no policy, nothing blocked"},
        "prompt": "safety first",
    }
    client, _ = make([ok({"data": payload})], clock)
    with pytest.raises(AtlasTerminal) as exc:
        client.poll("p")
    assert not isinstance(exc.value, AtlasRefused)


def test_a_rejected_submit_that_reads_like_moderation_is_a_refusal(clock):
    client, _ = make(
        [
            ok({"message": "request blocked by content policy"}, status=400),
            ok({"data": {"error": "moderation"}}, status=422),
            ok({"message": "bad request"}, status=400),
            ok({"data": {"id": "x", "message": "policy"}}, status=200),
        ],
        clock,
    )
    with pytest.raises(AtlasRefused, match="^generateImage failed: 400"):
        client.generate_image(IMG)
    with pytest.raises(AtlasRefused, match="^generateVideo failed: 422"):
        client.generate_video(VIDEO)
    with pytest.raises(AtlasError) as exc:
        client.generate_image(IMG)
    assert not isinstance(exc.value, AtlasRefused)
    assert client.generate_image(IMG) == "x"  # a successful submit is never read for refusal words


def test_poll_once_classifies_without_sleeping(clock):
    script = [
        ok({"data": {"status": "created"}}),
        ok({"data": {"status": "processing"}}),
        ok({"data": {"status": "completed", "outputs": ["https://cdn.atlascloud.ai/o.mp4"]}}),
        ok({"data": {"status": "failed", "message": "moderation"}}),
        ok({"data": {"status": "failed", "message": "oom"}}),
        TransportResponse(status=502, json=None, body=b"<html>"),
    ]
    client, transport = make(script, clock)
    states = [client.poll_once("p").state for _ in range(6)]
    assert states == ["queued", "running", "done", "refused", "failed", "running"]
    assert clock.sleeps == []
    assert transport.calls[0].url == f"{ATLAS_BASE}/model/prediction/p"
    assert transport.calls[0].headers == {"Authorization": f"Bearer {KEY}"}


def test_poll_once_returns_the_output_urls(clock):
    client, _ = make([ok({"data": {"outputs": ["https://cdn.atlascloud.ai/o.png"]}})], clock)
    pred = client.poll_once("p")
    assert (pred.state, pred.outputs) == ("done", ("https://cdn.atlascloud.ai/o.png",))


def test_polling_cadence_constants():
    assert (IMAGE_POLL_INTERVAL_S, IMAGE_POLL_TIMEOUT_S) == (3.0, 120.0)
    assert (VIDEO_POLL_INTERVAL_S, VIDEO_POLL_TIMEOUT_S) == (5.0, 720.0)


def test_image_polling_is_3_seconds_capped_at_120(clock):
    client, transport = make(lambda call: running(), clock)
    with pytest.raises(AtlasTimeout):
        client.poll_image("p")
    assert clock.sleeps == [3.0] * 40 and len(transport.calls) == 40


def test_video_polling_is_5_seconds_capped_at_12_minutes(clock):
    client, transport = make(lambda call: running(), clock)
    started = time.perf_counter()
    with pytest.raises(AtlasTimeout, match="timed out"):
        client.poll_video("p")
    assert time.perf_counter() - started < 1.0
    assert clock.sleeps == [5.0] * 144 and len(transport.calls) == 144  # 720 s / 5 s


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("walks slowly to the gate", "walks to the gate"),
        ("Slow push-in, gently drifting", "push-in, drifting"),
        ("a leisurely stroll", "a stroll"),
        ("an unhurried SLOW walk", "an walk"),
        ("slow-motion walk", "walk"),
        ("pushes in", "pushes in"),
        ("slower than usual", "slower than usual"),  # a different word, not a pace word
        ("walks gently", "walks"),
    ],
)
def test_strip_pace_words(prompt, expected):
    assert strip_pace_words(prompt) == expected


def test_image_tail_strips_pace_words_from_the_sent_prompt(clock):
    client, transport = make([ok({"data": {"id": "v"}})], clock)
    client.generate_video(
        {**VIDEO, "image_tail": "https://a/2.png", "prompt": "She walks slowly and gently to the gate."}
    )
    assert transport.calls[0].json["prompt"] == "She walks and to the gate."
    assert transport.calls[0].json["image_tail"] == "https://a/2.png"


def test_without_image_tail_the_prompt_is_left_alone(clock):
    client, transport = make([ok({"data": {"id": "v"}})], clock)
    client.generate_video({**VIDEO, "prompt": "a slow push-in"})
    assert transport.calls[0].json["prompt"] == "a slow push-in"


def test_image_tail_without_a_first_frame_is_a_client_error(clock):
    client, transport = make([ok({"data": {"id": "v"}})], clock)
    with pytest.raises(AtlasInputError, match="image_tail"):
        client.generate_video({"prompt": "p", "image_tail": "https://a/2.png"})
    assert transport.calls == []
