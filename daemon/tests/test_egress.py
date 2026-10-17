"""Egress (T14): custody before anything, the host allowlist, metadata stripping, opaque file names, the leak
scan and the outbound log, proven by capturing every request a FakeEngine job makes on an NDA project."""

from __future__ import annotations

import base64
import json
import re
import struct
import zlib
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from blake3 import blake3

from plate_daemon.adapter import PollResult
from plate_daemon.custody import CustodyViolation, Grade
from plate_daemon.egress import (
    Egress,
    EgressBlocked,
    EgressRun,
    FetchFailed,
    LeakBlocked,
    PreparedRequest,
    new_opaque_id,
)
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.projects import Project
from plate_engines.adapters.base import Adapter, JobStatus, Quote
from plate_engines.loader import load_manifests
from plate_engines.models import EngineSize, Route, host_allowed
from plate_pixels import icc

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "spec" / "examples" / "tierA_still_clubhouse"
API = "https://api.fake-vendor.test"
CDN = "https://cdn.fake-cdn.test"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
METADATA = {"tEXt", "iTXt", "zTXt", "eXIf", "tIME"}


# ---------------------------------------------------------------------------- PNGs with metadata


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def _insert_after_ihdr(png: bytes, extra: list[bytes]) -> bytes:
    out = [icc.PNG_SIGNATURE]
    for ctype, payload in icc.iter_png_chunks(png):
        out.append(_chunk(ctype, payload))
        if ctype == b"IHDR":
            out.extend(extra)
    return b"".join(out)


def _plate_png(secret: str, seed: int = 0) -> bytes:
    """A small 8-bit sRGB PNG with the PLATE ICC profile and every kind of metadata chunk naming `secret`."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, size=(9, 14, 3), dtype=np.uint8)
    ok, enc = cv2.imencode(".png", img)
    assert ok
    png = icc.embed_png_icc(enc.tobytes(), icc.SRGB_ICC)
    s = secret.encode("utf-8")
    return _insert_after_ihdr(
        png,
        [
            _chunk(b"tEXt", b"Title\x00" + s),
            _chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00<x:xmpmeta>" + s + b"</x:xmpmeta>"),
            _chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(s)),
            _chunk(b"eXIf", b"MM\x00*\x00\x00\x00\x08" + s),
            _chunk(b"tIME", struct.pack(">HBBBBB", 2026, 10, 17, 9, 41, 7)),
        ],
    )


def _jpeg() -> bytes:
    ok, enc = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    assert ok
    return enc.tobytes()


def _pixels(png: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)


def _pngs_in(body: bytes) -> list[bytes]:
    """Every PNG inside a request body: raw (multipart) or base64 inside JSON. Independent of egress code."""
    found: list[bytes] = []
    start = body.find(icc.PNG_SIGNATURE)
    while start != -1:
        pos = start + 8
        while pos + 8 <= len(body):
            length = struct.unpack(">I", body[pos : pos + 4])[0]
            ctype = body[pos + 4 : pos + 8]
            pos += 12 + length
            if ctype == b"IEND":
                found.append(body[start:pos])
                break
        start = body.find(icc.PNG_SIGNATURE, pos)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return found

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            raw = x.split(",", 1)[1] if x.startswith("data:") else x
            try:
                data = base64.b64decode(raw, validate=True)
            except ValueError:
                return
            if data.startswith(icc.PNG_SIGNATURE):
                found.append(data)

    walk(doc)
    return found


# ---------------------------------------------------------------------------- the fake vendor


class Recorder(httpx.MockTransport):
    """A vendor that records every request and answers from a FakeEngine. It also checks that the outbound
    log already holds the request (with no status yet) when the request arrives."""

    def __init__(self, log: OutboundLog, engine: FakeEngine | None = None) -> None:
        self.log = log
        self.engine = engine or FakeEngine(polls_until_done=2)
        self.requests: list[httpx.Request] = []
        self.logged_before: list[bool] = []
        self.uploads = 0
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append(request)
        last = self.log.entries()[-1] if self.log.entries() else {}
        self.logged_before.append(
            last.get("body_hash") == blake3(body).hexdigest() and last.get("status") is None
        )
        host, path = request.url.host, request.url.path
        if host == "api.fake-vendor.test" and path == "/v1/uploads":
            self.uploads += 1
            return httpx.Response(200, json={"file_id": f"file-{self.uploads}"})
        if host == "api.fake-vendor.test" and path == "/v1/jobs" and request.method == "POST":
            payload = json.loads(body)
            job = self.engine.submit(payload, request.headers["idempotency-key"])
            return httpx.Response(200, json={"id": job})
        m = re.fullmatch(r"/v1/jobs/([\w-]+)", path)
        if host == "api.fake-vendor.test" and m and request.method == "GET":
            res = self.engine.poll(m.group(1))
            out = {"status": res.status}
            if res.result_url:
                out["result_url"] = f"{CDN}/results/{res.result_url.removeprefix(RESULT_SCHEME)}.png"
            return httpx.Response(200, json=out)
        m = re.fullmatch(r"/results/([\w-]+)\.png", path)
        if host == "cdn.fake-cdn.test" and m:
            return httpx.Response(200, content=self.engine.fetch(RESULT_SCHEME + m.group(1)))
        if host == "api.fake-vendor.test" and path == "/v1/redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        if host == "api.fake-vendor.test" and path == "/v1/gone":
            return httpx.Response(410, json={"error": "expired"})
        return httpx.Response(404, json={"error": "no such endpoint"})


class HttpFakeAdapter:
    """The daemon's EngineAdapter protocol spoken over HTTP to the fake vendor, through Egress only."""

    def __init__(self, egress: Egress, run: EgressRun, route: Route, plate: bytes, mask: bytes) -> None:
        self.egress, self.run, self.route, self.plate, self.mask = egress, run, route, plate, mask

    def submit(self, payload: dict, idempotency_key: str) -> str:
        up = self.egress.send(
            self.run,
            self.route,
            PreparedRequest(
                "POST",
                f"{API}/v1/uploads",
                files={"file": ("tula-wot_clubhouse-c03_beauty.png", self.plate, "image/png")},
                data={"purpose": "edit"},
            ),
        )
        body = {
            **payload,
            "file_id": up.json()["file_id"],
            "reference": "data:image/png;base64," + base64.b64encode(self.plate).decode("ascii"),
            "mask_b64": base64.b64encode(self.mask).decode("ascii"),
        }
        resp = self.egress.send(
            self.run,
            self.route,
            PreparedRequest(
                "POST", f"{API}/v1/jobs", headers={"Idempotency-Key": idempotency_key}, json=body
            ),
        )
        return resp.json()["id"]

    def poll(self, provider_job_id: str) -> PollResult:
        doc = self.egress.send(
            self.run, self.route, PreparedRequest("GET", f"{API}/v1/jobs/{provider_job_id}")
        ).json()
        return PollResult(doc["status"], result_url=doc.get("result_url"))

    def fetch(self, result_url: str) -> bytes:
        return self.egress.fetch(result_url, self.route, run=self.run)

    def cancel(self, provider_job_id: str) -> None:  # pragma: no cover - protocol completeness
        raise NotImplementedError


# ---------------------------------------------------------------------------- fixtures


@pytest.fixture
def packet() -> dict:
    return json.loads((EXAMPLE / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def legend() -> dict:
    return json.loads((EXAMPLE / "id_legend.json").read_text(encoding="utf-8"))


@pytest.fixture
def nda_project(tmp_path) -> Project:
    return Project.create(tmp_path / "projects" / "tula-wot", "tula-wot", {"nda": True})


@pytest.fixture
def packet_dir(tmp_path) -> Path:
    d = tmp_path / "packets" / "tula-wot_clubhouse-c03_20261017"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def run(nda_project, packet, legend, packet_dir) -> EgressRun:
    return EgressRun.for_project(nda_project, "run_01", packet=packet, legend=legend, packet_dir=packet_dir)


@pytest.fixture
def vendor() -> Route:
    """A first-party route with confirmed written C1 terms: what NDA work may use once terms are filed."""
    return Route.model_validate(
        {
            "provider": "fakevendor",
            "endpoint": f"{API}/v1",
            "region": "test",
            "custody_grade": "C1",
            "custody_status": "confirmed",
            "terms_hash": "c" * 64,
            "upload_visibility": "private",
            "allowed_hosts": ["api.fake-vendor.test", ".fake-cdn.test"],
        }
    )


@pytest.fixture
def manifests():
    return load_manifests()


@pytest.fixture
def log(nda_project) -> OutboundLog:
    return OutboundLog.for_project(nda_project.root)


@pytest.fixture
def recorder(log) -> Recorder:
    return Recorder(log)


@pytest.fixture
def egress(settings, log, recorder) -> Egress:
    e = Egress(settings, log, recorder)
    yield e
    e.close()


def _json_post(body: dict, url: str = f"{API}/v1/echo", headers: dict | None = None) -> PreparedRequest:
    return PreparedRequest("POST", url, headers=headers or {}, json=body)


# ---------------------------------------------------------------------------- the packet-capture test


def test_nda_packet_capture(egress, run, vendor, recorder, log, packet, packet_dir, nda_project):
    """A whole FakeEngine job on an NDA project, every request captured at the transport."""
    assert run.nda and run.allowed_grades == {Grade.C0, Grade.C1}
    plate = _plate_png("tula-wot clubhouse-c03 " + packet["camera"]["name"] + " " + str(packet_dir))
    mask = _plate_png("tula-wot mask", seed=1)
    adapter = HttpFakeAdapter(egress, run, vendor, plate, mask)

    job = adapter.submit(
        {"prompt": "warm late-afternoon light on the facade, keep the sky", "seed": 7}, "idem-1"
    )
    for _ in range(10):
        res = adapter.poll(job)
        if res.status == "succeeded":
            break
    assert res.status == "succeeded"
    result = adapter.fetch(res.result_url)
    # T12 made FakeEngine results real PNGs (was the seed's b"PLATE-FAKE-RESULT" text).
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert recorder.engine.submit_count == 1

    reqs = recorder.requests
    assert [(r.method, r.url.path) for r in reqs][:2] == [("POST", "/v1/uploads"), ("POST", "/v1/jobs")]
    assert len(reqs) == 2 + recorder.engine.poll_count + 1

    secrets = [
        nda_project.project_id,
        "tula_wot",
        packet["shot_id"],
        packet["packet_id"],
        packet["camera"]["name"],
        "ARCH_clubhouse_facade",
        str(packet_dir),
        packet_dir.as_posix(),
        packet_dir.name,
    ]
    png_count = 0
    for r in reqs:
        # every host is in the route's allowlist
        assert host_allowed(r.url.host, vendor.allowed_hosts), r.url
        # nothing that identifies the project leaves: not in the URL, the headers or the body
        wire = (str(r.url) + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())).encode() + r.content
        for s in secrets:
            assert s.lower().encode() not in wire.lower(), (s, r.url)
        # every PNG payload is stripped of metadata, keeps its colour profile and its exact pixels
        for png in _pngs_in(r.content):
            png_count += 1
            types = set(icc.png_chunk_types(png))
            assert not types & METADATA, types
            assert icc.read_png_icc(png) == icc.SRGB_ICC
            original = plate if np.array_equal(_pixels(png), _pixels(plate)) else mask
            assert np.array_equal(_pixels(png), _pixels(original))
    assert png_count == 3  # the upload, the data URL and the raw base64 mask

    # file names are opaque ULIDs, never the original
    names = re.findall(rb'filename="([^"]+)"', reqs[0].content)
    assert len(names) == 1 and re.fullmatch(rb"[0-9A-HJKMNP-TV-Z]{26}\.png", names[0])

    # the outbound log: one entry per request, written before it left, with the exact body hash
    entries = log.entries()
    assert len(entries) == len(reqs)
    assert all(recorder.logged_before)
    for entry, r in zip(entries, reqs, strict=True):
        assert entry["body_hash"] == blake3(r.content).hexdigest()
        assert (entry["host"], entry["method"], entry["path"]) == (r.url.host, r.method, r.url.path)
        assert entry["bytes"] == len(r.content) and entry["status"] == 200 and entry["bytes_in"] > 0
        assert (
            entry["run_id"] == "run_01"
            and entry["route_id"] == "fakevendor"
            and entry["custody_grade"] == "C1"
        )
    assert entries[0]["opaque_ids"] == [names[0].decode().removesuffix(".png")]
    assert len(entries[1]["opaque_ids"]) == 2 and all(ULID_RE.match(i) for i in entries[1]["opaque_ids"])
    assert entries[2]["opaque_ids"] == []
    raw_log = log.path.read_text(encoding="utf-8")
    for s in secrets[:6]:
        assert s.lower() not in raw_log.lower()


def test_grade_d_route_on_nda_is_refused_before_any_transport_call(egress, run, recorder, log, manifests):
    atlas = manifests["seedream-5-pro"].routes[0]
    assert atlas.custody_grade == "D"
    req = PreparedRequest(
        "POST",
        "https://api.atlascloud.ai/api/v1/model/uploadMedia",
        files={"file": ("x.png", _plate_png("x"))},
    )
    with pytest.raises(CustodyViolation) as exc:
        egress.send(run, atlas, req)
    assert exc.value.code == "custody" and exc.value.grade is Grade.D
    with pytest.raises(CustodyViolation):
        egress.fetch("https://x.aliyuncs.com/out.png", atlas, run=run)
    with pytest.raises(CustodyViolation):
        egress.transport(run, atlas)
    with pytest.raises(CustodyViolation):
        egress.send(None, atlas, req)
    assert recorder.requests == [] and log.entries() == []
    assert not log.path.exists()


def test_pending_c1_route_counts_as_d_on_nda(egress, run, recorder, manifests):
    openai = manifests["gpt-image-2.5"].routes[0]
    assert (openai.custody_grade, openai.custody_status) == ("C1", "pending_written_terms")
    with pytest.raises(CustodyViolation, match="pending counts as D"):
        egress.send(run, openai, _json_post({"prompt": "x"}, "https://api.openai.com/v1/images/edits"))
    assert recorder.requests == []


def test_an_adapter_built_on_the_egress_transport_is_guarded(egress, run, recorder, log, manifests):
    """A plate_engines Adapter is handed Egress.transport(): its own http() client goes through the guard."""
    gpt = manifests["gpt-image-2.5"]
    route = gpt.routes[0].model_copy(update={"custody_status": "confirmed", "terms_hash": "d" * 64})
    manifest = gpt.model_copy(update={"routes": [route]})

    class Edits(Adapter):
        def quote(self, inputs, params):  # pragma: no cover - not exercised
            return Quote(
                engine_id=self.engine_id,
                route_id=self.route_id,
                price_usd=0,
                eta_s=0,
                custody_grade="C1",
                size=EngineSize(w=0, h=0, label="auto"),
                model_version="x",
            )

        def submit(self, staged):
            r = self.http().post(
                "https://api.openai.com/v1/images/edits",
                files=[("image[]", ("tula-wot_plate.png", staged.inputs["image"], "image/png"))],
                data={"model": "gpt-image-2.5", "prompt": staged.params["prompt"]},
            )
            return r.json().get("error", "job")

        def poll(self, provider_job_id):  # pragma: no cover - not exercised
            return JobStatus(state="done")

    plate = _plate_png("tula-wot")
    adapter = Edits(manifest, route, transport=egress.transport(run, route))
    from plate_engines.adapters.base import Staged

    adapter.submit(Staged(inputs={"image": plate}, params={"prompt": "dusk"}))
    assert len(recorder.requests) == 1
    sent = recorder.requests[0].content
    assert b"tula-wot" not in sent.lower()
    assert re.search(rb'filename="[0-9A-HJKMNP-TV-Z]{26}\.png"', sent)
    (png,) = _pngs_in(sent)
    assert not set(icc.png_chunk_types(png)) & METADATA
    (entry,) = log.entries()
    assert entry["body_hash"] == blake3(sent).hexdigest() and entry["status"] == 404
    with pytest.raises(LeakBlocked):
        adapter.http().post(
            "https://api.openai.com/v1/images/edits", json={"prompt": "the tula-wot clubhouse"}
        )
    assert len(recorder.requests) == 1
    adapter.close()


def test_sdk_client_goes_through_the_guard(egress, run, vendor, recorder, log):
    with egress.client(run, vendor) as c:
        r = c.post(f"{API}/v1/uploads", files={"file": ("plate.png", _plate_png("x"), "image/png")})
    assert r.status_code == 200 and len(recorder.requests) == 1 and len(log.entries()) == 1


# ---------------------------------------------------------------------------- destination


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/jobs",
        "https://api.fake-vendor.test.evil.example/v1/jobs",  # suffix trick
        "https://evilapi.fake-vendor.test/v1/jobs",  # not a ".suffix" entry: exact host only
        "http://api.fake-vendor.test/v1/jobs",
        "https://api.fake-vendor.test:8443/v1/jobs",
        "https://user:pw@api.fake-vendor.test/v1/jobs",
    ],
)
def test_destination_outside_the_allowlist_is_blocked(egress, run, vendor, recorder, log, url):
    with pytest.raises(EgressBlocked) as exc:
        egress.send(run, vendor, _json_post({"prompt": "x"}, url))
    assert not isinstance(exc.value, LeakBlocked)
    assert recorder.requests == [] and log.entries() == []


def test_a_route_with_no_allowed_hosts_sends_nothing(egress, run, recorder, manifests):
    local = manifests["qwen-image-edit-2509"].routes[0]
    assert local.custody_grade == "C0" and local.allowed_hosts == []
    with pytest.raises(EgressBlocked, match="no allowed_hosts"):
        egress.send(run, local, _json_post({"prompt": "x"}, f"{API}/v1/jobs"))
    assert recorder.requests == []


def test_fetch_enforces_the_download_allowlist_and_status(egress, run, vendor, recorder):
    with pytest.raises(EgressBlocked):
        egress.fetch("https://evil.example/results/x.png", vendor, run=run)
    with pytest.raises(FetchFailed, match="410"):
        egress.fetch(f"{API}/v1/gone", vendor, run=run)
    assert len(recorder.requests) == 1
    assert "authorization" not in {k.lower() for k in recorder.requests[0].headers}


def test_redirects_are_not_followed(egress, run, vendor, recorder):
    r = egress.send(run, vendor, PreparedRequest("GET", f"{API}/v1/redirect"))
    assert r.status_code == 302 and len(recorder.requests) == 1


# ---------------------------------------------------------------------------- the leak scan


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "the TULA WOT clubhouse at dusk"},  # slug, other case, space between words
        {"prompt": "tulawot"},
        {"prompt": "shot clubhouse_c03 again"},
        {"note": "CAM_C03_clubhouse_entry"},
        {"layers": ["ARCH_clubhouse_facade"]},
        {"material": "mat_brick_wirecut"},
        {"ref": "pkt_01JA7Q3M8Z4X6V2C9D5E1F0GHK"},
        {"tula-wot": 1},  # keys are scanned too
    ],
)
def test_project_identifiers_in_a_body_are_blocked(egress, run, vendor, recorder, log, body):
    with pytest.raises(LeakBlocked):
        egress.send(run, vendor, _json_post(body))
    assert recorder.requests == [] and log.entries() == []


def test_paths_headers_queries_and_form_fields_are_scanned(
    egress, run, vendor, recorder, packet_dir, settings
):
    cases = [
        _json_post({"src": str(packet_dir).replace("/", "\\")}),  # either separator
        _json_post({"src": packet_dir.name + ".zip"}),
        _json_post({"x": 1}, headers={"X-Camera": "CAM_C03_clubhouse_entry"}),
        _json_post({"x": 1}, url=f"{API}/v1/jobs?shot=clubhouse-c03"),
        _json_post({"x": 1}, url=f"{API}/v1/jobs?shot=clubhouse%2Dc03"),
        PreparedRequest("POST", f"{API}/v1/jobs", data={"prompt": "tula-wot"}),
        PreparedRequest(
            "POST", f"{API}/v1/uploads", files={"tula-wot": ("a.png", _plate_png("x"), "image/png")}
        ),
        _json_post({"cache": str(settings.home / "cache" / "a.png")}),  # this machine's PLATE folder
        _json_post({"home": str(Path.home() / "Desktop")}),
    ]
    for req in cases:
        with pytest.raises(LeakBlocked):
            egress.send(run, vendor, req)
    assert recorder.requests == []


def test_plain_words_and_spaced_layer_names_are_not_identifiers(egress, run, vendor, recorder):
    body = {"prompt": "the arch facade under a clear sky, sett paving and palms; edit 2 of mask"}
    assert egress.send(run, vendor, _json_post(body)).status_code == 404  # sent: the fake has no such path
    assert len(recorder.requests) == 1


def test_a_colour_profile_named_after_the_project_is_blocked(egress, run, vendor, recorder):
    png = icc.embed_png_icc(_plate_png("x"), icc.SRGB_ICC, name="tula-wot")
    with pytest.raises(LeakBlocked):
        egress.send(
            run,
            vendor,
            PreparedRequest("POST", f"{API}/v1/uploads", files={"file": ("a.png", png, "image/png")}),
        )
    assert recorder.requests == []


def test_machine_paths_are_blocked_on_every_project(settings, tmp_path, vendor):
    project = Project.create(tmp_path / "own", "own-scene", {"nda": False})
    log = OutboundLog.for_project(project.root)
    rec = Recorder(log)
    run = EgressRun.for_project(project, "run_02")
    assert not run.nda and run.allowed_grades == set(Grade)
    e = Egress(settings, log, rec)
    with pytest.raises(LeakBlocked):
        e.send(run, vendor, _json_post({"p": str(settings.packets_dirs[0] / "x")}))
    assert e.send(run, vendor, _json_post({"p": "fine"})).status_code == 404
    assert len(rec.requests) == 1


# ---------------------------------------------------------------------------- sanitising


def test_jpeg_and_other_formats_never_leave(egress, run, vendor, recorder):
    jpeg = _jpeg()
    cases = [
        PreparedRequest("POST", f"{API}/v1/uploads", files={"file": ("a.png", jpeg, "image/png")}),
        _json_post({"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}),
        _json_post({"image": base64.b64encode(jpeg).decode()}),
        PreparedRequest("POST", f"{API}/v1/uploads", content=b"II*\x00" + b"\x00" * 64),
    ]
    for req in cases:
        with pytest.raises(LeakBlocked, match="PNG only"):
            egress.send(run, vendor, req)
    assert recorder.requests == []


def test_unknown_binary_is_refused_on_nda_but_passes_elsewhere(
    egress, run, vendor, recorder, settings, tmp_path
):
    blob = bytes(range(256)) * 4
    req = PreparedRequest("POST", f"{API}/v1/uploads", files={"file": ("clip.mp4", blob, "video/mp4")})
    with pytest.raises(LeakBlocked, match="NDA"):
        egress.send(run, vendor, req)
    with pytest.raises(LeakBlocked, match="NDA"):
        egress.send(run, vendor, _json_post({"video": base64.b64encode(blob).decode()}))
    assert recorder.requests == []
    project = Project.create(tmp_path / "own", "own-scene", {"nda": False})
    open_run = EgressRun.for_project(project, "run_03")
    egress.send(open_run, vendor, req)
    (sent,) = recorder.requests
    assert re.search(rb'filename="[0-9A-HJKMNP-TV-Z]{26}\.mp4"', sent.content) and blob in sent.content


def test_bodies_that_cannot_be_read_are_refused(egress, run, vendor, recorder):
    cases = [
        PreparedRequest(
            "POST", f"{API}/v1/jobs", headers={"Content-Type": "application/json"}, content=b"{nope"
        ),
        PreparedRequest(
            "POST",
            f"{API}/v1/jobs",
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=zlib.compress(b'{"a": 1}'),
        ),
        PreparedRequest(
            "POST", f"{API}/v1/uploads", headers={"Content-Type": "multipart/form-data"}, content=b"--x\r\n"
        ),
    ]
    for req in cases:
        with pytest.raises(LeakBlocked, match="cannot be checked"):
            egress.send(run, vendor, req)
    assert recorder.requests == []


def test_a_raw_png_body_is_stripped(egress, run, vendor, recorder, log):
    png = _plate_png("tula-wot")
    egress.send(
        run,
        vendor,
        PreparedRequest("PUT", f"{API}/v1/uploads", headers={"Content-Type": "image/png"}, content=png),
    )
    sent = recorder.requests[0].content
    assert not set(icc.png_chunk_types(sent)) & METADATA and np.array_equal(_pixels(sent), _pixels(png))
    assert len(log.entries()[0]["opaque_ids"]) == 1


def test_urlsafe_base64_png_inside_json_is_stripped(egress, run, vendor, recorder):
    png = _plate_png("tula-wot", seed=3)
    egress.send(run, vendor, _json_post({"img": base64.urlsafe_b64encode(png).decode()}))
    sent = json.loads(recorder.requests[0].content)["img"]
    clean = base64.urlsafe_b64decode(sent)
    assert not set(icc.png_chunk_types(clean)) & METADATA and np.array_equal(_pixels(clean), _pixels(png))


# ---------------------------------------------------------------------------- the outbound log


def test_transport_failure_is_logged_and_raised(settings, log, run, vendor):
    def boom(request):
        raise httpx.ConnectError("vendor unreachable", request=request)

    e = Egress(settings, log, httpx.MockTransport(boom))
    with pytest.raises(httpx.ConnectError):
        e.send(run, vendor, _json_post({"prompt": "x"}))
    (entry,) = log.entries()
    assert entry["status"] is None and "vendor unreachable" in entry["error"]


def test_the_query_string_is_never_logged(egress, run, vendor, log):
    egress.send(run, vendor, PreparedRequest("GET", f"{API}/v1/jobs/job-1?key=SECRET-API-KEY"))
    assert "SECRET-API-KEY" not in log.path.read_text(encoding="utf-8")
    assert log.entries()[0]["path"] == "/v1/jobs/job-1"


def test_log_survives_a_torn_line_and_tails(tmp_path):
    log = OutboundLog(tmp_path / "outbound.jsonl")
    log.request(
        entry_id="A",
        run_id="r",
        route_id="x",
        host="h",
        method="post",
        path="/",
        bytes_out=1,
        custody_grade="C1",
        opaque_ids=[],
        body_hash="0" * 64,
    )
    with open(log.path, "ab") as fh:
        fh.write(b'{"kind": "response", "id": "A", "sta')  # a crash mid-append
    for i in range(3):
        log.request(
            entry_id=f"B{i}",
            run_id="r",
            route_id="x",
            host="h",
            method="get",
            path="/",
            bytes_out=0,
            custody_grade="C1",
            opaque_ids=[],
            body_hash="1" * 64,
        )
    log.response("B2", status=201, bytes_in=5)
    records, problems = log.records()
    assert len(problems) == 1 and len(records) == 5
    entries = log.entries()
    assert [e["id"] for e in entries] == ["A", "B0", "B1", "B2"] and entries[0]["method"] == "POST"
    assert entries[0]["status"] is None and entries[-1]["status"] == 201
    assert [e["id"] for e in log.tail(2)] == ["B1", "B2"] and log.tail(0) == []
    assert all(line.endswith("}") for line in log.path.read_text(encoding="utf-8").splitlines()[2:])


# ---------------------------------------------------------------------------- ids, runs, and the one door


def test_opaque_ids_are_ulids():
    ids = [new_opaque_id() for _ in range(200)]
    assert len(set(ids)) == 200 and all(ULID_RE.match(i) for i in ids)
    assert new_opaque_id(0, b"\x00" * 10) == "0" * 26
    assert new_opaque_id(1, b"\x00" * 10) < new_opaque_id(2, b"\x00" * 10)


def test_run_for_project_collects_identifiers_not_plain_words(nda_project, packet, legend, packet_dir):
    graph = {
        "nodes": [
            {"id": "n1", "data": {"kind": "Mask", "label": "Mask 2"}},
            {"id": "n2", "data": {"kind": "Edit", "label": "Terrace dusk relight"}},
            {"id": "n3", "data": {"kind": "Edit", "label": "Terrace"}},
        ],
        "edges": [],
    }
    nda_project.save_graph(graph)
    run = EgressRun.for_project(nda_project, "r", packet=packet, legend=legend, packet_dir=packet_dir)
    assert {"tula-wot", "clubhouse-c03"} <= set(run.slugs)
    assert {
        packet["packet_id"],
        packet["camera"]["name"],
        "ARCH_facade",
        "MAT_brick_wirecut",
        "Box042",
        "Terrace dusk relight",
    } <= set(run.terms)
    lowered = {t.lower() for t in run.terms}
    assert not lowered & {"mask 2", "terrace", "0"}
    assert packet_dir.name in run.paths and str(packet_dir) in run.paths


def test_no_httpx_client_outside_egress_and_transport():
    """The custody claim 'nothing leaves except through Egress' as a grep over daemon/ and engines/."""
    pattern = re.compile(r"httpx\.(Async)?Client\(")
    offenders = []
    for top in ("daemon", "engines"):
        for path in sorted((REPO / top).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in f"/{rel}" or path.name in ("egress.py", "transport.py") or ".venv" in path.parts:
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert offenders == [], "build vendor clients through Egress (or transport.make_client): " + "; ".join(
        offenders
    )
