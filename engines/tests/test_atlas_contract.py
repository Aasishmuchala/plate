"""Contract tests for the Atlas client: one test per quirk of ~/Desktop/staffage/src/lib/atlas.ts.

Everything runs against FakeTransport with a recording sleep and a fake clock, so nothing touches the network
and nothing really sleeps. Line numbers in the docstrings refer to atlas.ts.
"""

from __future__ import annotations

import time

import pytest

from plate_engines.atlas import (
    ATLAS_BASE,
    RESULT_HOSTS,
    AtlasClient,
    AtlasError,
    AtlasTerminal,
    AtlasTimeout,
    FakeTransport,
    TransportResponse,
)

KEY = "test-key-123"


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
        client.generate_image({"prompt": "x"})
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
    assert client.generate_image({}) == "top"
    assert client.generate_image({}) == "12345"
    assert client.generate_image({}) == "n"


def test_generate_image_failures(clock):
    client, _ = make(
        [ok({"message": "bad"}, status=400), ok({"data": {}}), ok({"id": "x"}, status=500)], clock
    )
    with pytest.raises(AtlasError, match=r'^generateImage failed: 400 \{"message":"bad"\}$'):
        client.generate_image({})
    with pytest.raises(AtlasError, match="^generateImage failed: 200"):
        client.generate_image({})
    with pytest.raises(AtlasError, match="^generateImage failed: 500"):
        client.generate_image({})


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
