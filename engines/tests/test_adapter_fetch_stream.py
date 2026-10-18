"""Result downloads stream under a byte cap and a wall-clock deadline (router plan T130, decision 76).

The engine-faults attacker served a 300 MB body and a 6-byte body at one byte per 20 s through the production
stack: the first sat whole in memory before anything looked at it, the second took 120 s past a 60 s timeout
(httpx's timeout is per read). `Adapter.fetch` now refuses a `Content-Length` over the cap at once, counts the
body into a spool and stops at the cap or the deadline, whatever one read does. Every vendor here is an
`httpx.MockTransport` handler; the network stays blocked (conftest).
"""

from __future__ import annotations

import threading
import time
import tracemalloc

import httpx
import pytest
from blake3 import blake3

from plate_engines.adapters.base import (
    FETCH_DEADLINE_S,
    FETCH_MAX_BYTES,
    Adapter,
    FetchError,
    JobStatus,
    stream_download,
)
from plate_engines.adapters.transport import HostNotAllowed, make_client
from plate_engines.models import EngineSize

MiB = 1024 * 1024
HOST = "https://img.atlascloud.ai"


class Probe(Adapter):
    """The smallest concrete adapter: the base fetch is what is under test."""

    def quote(self, inputs, params):  # pragma: no cover - not exercised
        return self.make_quote(0.0, 0.0, EngineSize(w=0, h=0, label="auto"))

    def submit(self, staged) -> str:  # pragma: no cover - not exercised
        return "job-1"

    def poll(self, provider_job_id: str) -> JobStatus:  # pragma: no cover - not exercised
        return JobStatus(state="done")


class Body(httpx.SyncByteStream):
    """A response body: `n` chunks of `chunk`, optionally waiting `gap_s` before each (on an Event, so a test's
    teardown releases a stalled reader at once). Counts what it yielded and whether it was closed."""

    def __init__(self, chunk: bytes, n: int, *, gap_s: float = 0.0, release: threading.Event | None = None):
        self.chunk, self.n, self.gap_s = chunk, n, gap_s
        self.release = release or threading.Event()
        self.yielded = 0
        self.closed = threading.Event()

    def __iter__(self):
        for _ in range(self.n):
            if self.gap_s and self.release.wait(self.gap_s):
                return
            self.yielded += 1
            yield self.chunk

    def close(self) -> None:
        self.closed.set()


@pytest.fixture
def release():
    ev = threading.Event()
    yield ev
    ev.set()  # never leave a reading thread asleep after the test


def adapter(manifests, handler, engine: str = "kling-o3-4k", route: str = "atlas", **limits) -> Probe:
    a = Probe(manifests[engine], route, transport=httpx.MockTransport(handler))
    for k, v in limits.items():
        setattr(a, k, v)
    return a


def serve(body: httpx.SyncByteStream | bytes, *, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(status, stream=body, headers=headers)

    return handler


# ---------------------------------------------------------------------------- the caps


def test_the_cap_follows_the_manifest_kind_and_an_instance_override(manifests):
    assert FETCH_MAX_BYTES == {"image": 256 * MiB, "video": 2048 * MiB} and FETCH_DEADLINE_S == 600.0
    image = adapter(manifests, serve(b""), engine="nano-banana-2.1")
    video = adapter(manifests, serve(b""))
    assert manifests["nano-banana-2.1"].kind == "image_edit" and image.fetch_cap() == 256 * MiB
    assert manifests["kling-o3-4k"].kind == "video_startend" and video.fetch_cap() == 2048 * MiB
    assert video.fetch_deadline_s == 600.0
    video.fetch_max_bytes = 64 * MiB
    assert video.fetch_cap() == 64 * MiB


def test_a_small_result_comes_back_byte_for_byte(manifests):
    data = blake3(b"result").digest(length=5000)
    a = adapter(manifests, serve(data))
    assert a.fetch(f"{HOST}/out/o1.png") == data
    chunked = Body(b"abc", 7)
    assert adapter(manifests, serve(chunked)).fetch(f"{HOST}/out/o2.png") == b"abc" * 7
    assert chunked.closed.is_set()  # the response is closed once read


def test_a_body_past_the_in_memory_spool_is_returned_intact(manifests):
    block = blake3(b"spool").digest(length=MiB)
    body = Body(block, 20)  # 20 MiB: past the 8 MiB the spool keeps in memory
    out = adapter(manifests, serve(body)).fetch(f"{HOST}/out/big.mp4")
    assert len(out) == 20 * MiB and out == block * 20


def test_exactly_the_cap_passes_and_one_byte_more_fails(manifests):
    a = adapter(manifests, serve(Body(b"x" * 1000, 10)), fetch_max_bytes=10_000)
    assert len(a.fetch(f"{HOST}/out/a.png")) == 10_000
    b = adapter(manifests, serve(Body(b"x" * 1000, 10)), fetch_max_bytes=9_999)
    with pytest.raises(FetchError, match=r"^download exceeded 9999 bytes$"):
        b.fetch(f"{HOST}/out/a.png")


def test_a_content_length_over_the_cap_is_refused_before_the_body(manifests):
    body = Body(b"\x00" * MiB, 300)
    a = adapter(manifests, serve(body, headers={"Content-Length": str(300 * MiB)}), fetch_max_bytes=64 * MiB)
    with pytest.raises(FetchError, match=r"download exceeded 67108864 bytes \(Content-Length 314572800\)"):
        a.fetch(f"{HOST}/out/huge.png")
    assert body.closed.wait(2)
    assert body.yielded == 0  # refused on the header: not one byte of the body was read


def test_a_300_mb_chunked_body_stops_at_a_64_mib_cap_with_bounded_memory(manifests):
    """The attacker's big300_chunked case: no Content-Length, 300 x 1 MiB chunks."""
    body = Body(b"\x00" * MiB, 300)
    a = adapter(manifests, serve(body), fetch_max_bytes=64 * MiB)
    tracemalloc.start()
    try:
        t0 = time.monotonic()
        with pytest.raises(FetchError, match=r"^download exceeded 67108864 bytes$"):
            a.fetch(f"{HOST}/out/big300.png")
        elapsed = time.monotonic() - t0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100 * MiB, f"peak {peak / MiB:.0f} MiB"  # the old path held all 300 MB
    assert body.closed.wait(5) and body.yielded < 75  # the reader stopped a few chunks past the cap
    assert elapsed < 30


def test_a_drip_feed_hits_the_wall_clock_deadline(manifests, release):
    """The attacker's drip case: Content-Length 6, one byte every 20 s. httpx's per-read timeout never trips."""
    body = Body(b"x", 6, gap_s=20.0, release=release)
    a = adapter(manifests, serve(body, headers={"Content-Length": "6"}), fetch_deadline_s=5.0)
    t0 = time.monotonic()
    with pytest.raises(FetchError, match=r"^download exceeded 5 s \(0 bytes in\)$"):
        a.fetch(f"{HOST}/out/drip.png")
    assert time.monotonic() - t0 < 7.0
    release.set()
    assert body.closed.wait(5)  # released, the reading thread closes the response and ends


def test_a_vendor_that_never_answers_hits_the_deadline_too(manifests, release):
    def handler(request):
        release.wait(30)  # the headers never come
        return httpx.Response(200, content=b"late")

    a = adapter(manifests, handler, fetch_deadline_s=1.0)
    t0 = time.monotonic()
    with pytest.raises(FetchError, match=r"^download exceeded 1 s"):
        a.fetch(f"{HOST}/out/never.png")
    assert time.monotonic() - t0 < 3.0


def test_status_host_and_transport_errors_keep_their_meaning(manifests):
    a = adapter(manifests, serve(b"gone", status=404))
    with pytest.raises(FetchError, match=r"^download failed: 404$"):
        a.fetch(f"{HOST}/out/gone.png")
    calls: list[str] = []

    def record(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"x")

    with pytest.raises(HostNotAllowed):
        adapter(manifests, record).fetch("https://evil.example/out.png")
    assert calls == []  # the allowlist refused before the transport

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"half"
            raise httpx.ReadError("connection reset mid-body")

    with pytest.raises(httpx.ReadError, match="mid-body"):
        adapter(manifests, serve(Broken())).fetch(f"{HOST}/out/broken.png")


def test_a_redirect_is_never_followed(manifests):
    a = adapter(manifests, serve(b"", status=302, headers={"Location": "https://evil.example/x.png"}))
    with pytest.raises(FetchError, match=r"^download failed: 302$"):
        a.fetch(f"{HOST}/out/moved.png")


def test_stream_download_works_on_any_client(manifests):
    client = make_client(httpx.MockTransport(serve(b"plain bytes")))
    assert stream_download(client, f"{HOST}/x", max_bytes=100, deadline_s=5) == b"plain bytes"
    with pytest.raises(FetchError, match="exceeded 5 bytes"):
        stream_download(client, f"{HOST}/x", max_bytes=5, deadline_s=5)
    client.close()


def test_adapter_subclass_fetches_ride_the_same_stream(manifests):
    """The Atlas and Runway video adapters check their own URL rules, then call the base fetch."""
    from plate_engines.adapters.atlas_video import KlingStartEnd
    from plate_engines.adapters.runway_video import RunwayVideo

    body = Body(b"\x00" * MiB, 300)
    a = RunwayVideo(manifests["kling-o3-4k"], "runway", transport=httpx.MockTransport(serve(body)))
    a.fetch_max_bytes = 8 * MiB
    with pytest.raises(FetchError, match="download exceeded 8388608 bytes"):
        a.fetch("https://d1.cloudfront.net/task/out.mp4")
    assert body.closed.wait(5) and body.yielded < 20
    atlas = Body(b"\x00" * MiB, 300)
    k = KlingStartEnd(manifests["kling-o3-4k"], "atlas", transport=httpx.MockTransport(serve(atlas)))
    k.fetch_max_bytes = 4 * MiB
    with pytest.raises(FetchError, match="download exceeded 4194304 bytes"):
        k.fetch(f"{HOST}/out/clip.mp4")
