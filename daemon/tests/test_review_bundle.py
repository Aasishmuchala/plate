"""Client review bundles (T40): static HTML wipe page, watermarked proxies, gate badges, comment form."""

from __future__ import annotations

import json
import re
import struct
import zipfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pytest

from plate_daemon import lineage as L
from plate_daemon import review_bundle as rb
from plate_daemon.gate_reports import Service as GateReports
from plate_daemon.ledger import Ledger
from plate_daemon.projects import Project
from plate_pixels import imageio

REPO = Path(__file__).resolve().parents[2]
PROJECT_ID = "acme-clubhouse-7f3a"
LIN = L.Lineage(
    project_id=PROJECT_ID,
    node_id="node_7",
    run_id="run_cand",
    packet_id="pkt_room_a",
    engine_id="nano-banana-2.1",
    created_utc="2026-10-17T09:41:07.123456Z",
)


def gradient(w: int, h: int, phase: float = 0.0) -> np.ndarray:
    """A smooth, textured float image so a downscale or a watermark visibly changes pixels."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 0.5 + 0.4 * np.sin(x / 37.0 + phase)
    g = 0.5 + 0.4 * np.cos(y / 29.0 - phase)
    b = 0.5 + 0.3 * np.sin((x + y) / 53.0)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 1).astype(np.float32)


def failing_report() -> dict:
    return {
        "gate_version": "0.1.0",
        "metric_code_hash": "ab" * 16,
        "threshold_set_id": "ts-test",
        "tier": "A",
        "verdict": "fail",
        "checks": [
            {"metric_id": "registration.residual_px", "value": 0.4, "status": "pass"},
            {"metric_id": "advisory.thing", "value": 0.7, "status": "advisory"},
            {"metric_id": "texture.lapvar", "value": 212.0, "status": "fail"},
            {"metric_id": "outside_mask.de00", "value": 3.0, "status": "fail"},
        ],
    }


class Rig:
    """A project folder with runs whose plate and candidate sit in its CAS and whose reports sit in gates/."""

    def __init__(self, tmp_path: Path, nda: bool = False):
        self.tmp = tmp_path
        root = tmp_path / "proj"
        root.mkdir()
        self.project = Project.create(root, PROJECT_ID, {"nda": nda})
        self.ledger = Ledger(root)
        self.cas = self.project.cas()
        self.gates = GateReports(root)
        self.out = tmp_path / "out"

    def add_run(
        self,
        run_id: str,
        size: tuple[int, int] = (2400, 1350),
        report: dict | None = None,
        lineage: bool = True,
        phase: float = 0.0,
        plate_size: tuple[int, int] | None = None,
    ) -> str:
        w, h = size
        plate = self.tmp / f"{run_id}_plate.png"
        cand = self.tmp / f"{run_id}_cand.png"
        pw, ph = plate_size or size
        imageio.write_png16(plate, gradient(pw, ph))
        imageio.write_png16(cand, gradient(w, h, phase + 0.6))
        if lineage:
            L.embed(
                cand,
                L.Lineage(
                    PROJECT_ID, "node_7", run_id, "pkt_room_a", "nano-banana-2.1", created_utc=LIN.created_utc
                ),
            )
        ck, pk = self.cas.put_file(cand, "png"), self.cas.put_file(plate, "png")
        self.ledger.append(kind="done", run_id=run_id, data={"result_key": ck, "plate_key": pk})
        if report is not None:
            self.gates.write(run_id, report)
        return run_id


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def build_three(rig: Rig) -> rb.ReviewBundle:
    from plate_gates.report import GateReport

    rig.add_run("run_fail", report=failing_report())
    rig.add_run(
        "run_pass", size=(1600, 900), report=GateReport.score({"registration.residual_px": 0.3}, tier="A")
    )
    rig.add_run("run_none", size=(800, 450))
    return rb.build_review(
        rig.project, ["run_fail", "run_pass", "run_none"], rig.out, title="Clubhouse stills"
    )


def open_zip(bundle: rb.ReviewBundle) -> zipfile.ZipFile:
    return zipfile.ZipFile(bundle.zip_path)


def page_data(zf: zipfile.ZipFile) -> dict:
    index = next(n for n in zf.namelist() if n.endswith("/index.html"))
    text = zf.read(index).decode("utf-8")
    m = re.search(r'<script id="plate-review-data" type="application/json">(.*?)</script>', text, re.S)
    assert m, "the data blob is missing"
    return json.loads(m.group(1))


def png_header(raw: bytes) -> tuple[int, int, int]:
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth = struct.unpack(">IIB", raw[16:25])
    return w, h, depth


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, dict[str, str | None]] = {}
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        self.tags.append((tag, d))
        if "id" in d:
            self.ids[str(d["id"])] = d


# ----------------------------------------------------------------------------- structure and contents


def test_bundle_has_both_images_and_a_badge_per_take(rig: Rig) -> None:
    bundle = build_three(rig)
    zf = open_zip(bundle)
    names = zf.namelist()
    assert len(bundle.takes) == 3
    data = page_data(zf)
    assert [t["label"] for t in data["takes"]] == ["r1", "r2", "r3"]
    folder = f"review-{bundle.bundle_id}"
    for take in data["takes"]:
        assert f"{folder}/{take['a']}" in names and f"{folder}/{take['b']}" in names
        assert take["a"].endswith("_a.png") and take["b"].endswith("_b.png")
        assert take["badge"]["status"] in ("pass", "fail", "advisory", "none")
        assert f"{folder}/takes/{take['label']}_gate.json" in names
    statuses = [t["badge"]["status"] for t in data["takes"]]
    assert statuses == ["fail", "pass", "none"]
    assert {"index.html", "README.txt", "manifest.json"} <= {n.split("/", 1)[1] for n in names}


def test_badge_names_the_first_failing_metric(rig: Rig) -> None:
    bundle = build_three(rig)
    zf = open_zip(bundle)
    badge = page_data(zf)["takes"][0]["badge"]
    assert badge["first_failing"] == {"metric_id": "texture.lapvar", "value": 212.0, "status": "fail"}
    assert (badge["n_checks"], badge["n_fail"], badge["n_advisory"]) == (4, 2, 1)
    sidecar_gate = json.loads(zf.read(f"review-{bundle.bundle_id}/takes/r1_gate.json"))
    assert sidecar_gate["take"] == "r1" and sidecar_gate["status"] == "fail"
    passed = page_data(zf)["takes"][1]["badge"]
    assert passed["status"] == "pass" and passed["first_failing"] is None and passed["tier"] == "A"
    assert page_data(zf)["takes"][2]["badge"]["status"] == "none"


def test_advisory_badge_names_the_first_advisory_metric() -> None:
    rep = {
        "verdict": "advisory",
        "checks": [
            {"metric_id": "a", "value": 1, "status": "pass"},
            {"metric_id": "texture.fine_detail_ratio", "value": 0.7, "status": "advisory"},
        ],
    }
    b = rb.gate_badge(rep, report_n=2)
    assert b["status"] == "advisory" and b["first_failing"]["metric_id"] == "texture.fine_detail_ratio"
    assert b["report_n"] == 2
    derived = rb.gate_badge({"checks": [{"metric_id": "x", "value": None, "status": "fail"}]})
    assert derived["status"] == "fail" and derived["first_failing"]["value"] is None


def test_no_png_wider_than_2048_and_nothing_16_bit(rig: Rig) -> None:
    bundle = build_three(rig)
    zf = open_zip(bundle)
    pngs = [n for n in zf.namelist() if n.endswith(".png")]
    assert len(pngs) == 6
    for n in pngs:
        w, h, depth = png_header(zf.read(n))
        assert w <= 2048, n
        assert depth == 8, f"{n} is {depth}-bit: a review bundle carries no master"
    assert not [n for n in zf.namelist() if n.lower().endswith((".exr", ".tif", ".tiff", ".mp4", ".mov"))]
    # the 2400 px source is brought down to 2048; the 1600 and 800 px ones are never upscaled
    widths = [png_header(zf.read(f"review-{bundle.bundle_id}/takes/r{i}_b.png"))[0] for i in (1, 2, 3)]
    assert widths == [2048, 1600, 800]


def test_max_width_is_honoured_and_capped(rig: Rig) -> None:
    rig.add_run("run_a")
    b = rb.build_review(rig.project, ["run_a"], rig.out, max_width=1000)
    w, h, _ = png_header(open_zip(b).read(f"review-{b.bundle_id}/takes/r1_a.png"))
    assert (w, h) == (1000, 562)
    for bad in (2049, 63, 0, True, 1000.5):
        with pytest.raises(ValueError):
            rb.build_review(rig.project, ["run_a"], rig.out, max_width=bad)  # type: ignore[arg-type]


def test_no_file_name_or_page_text_holds_the_project_id(rig: Rig) -> None:
    bundle = build_three(rig)
    zf = open_zip(bundle)
    for n in zf.namelist() + [bundle.zip_path.name]:
        assert PROJECT_ID not in n and "run_" not in n, n
    for n in zf.namelist():
        if n.endswith((".html", ".txt", ".json")):
            text = zf.read(n).decode("utf-8")
            assert PROJECT_ID not in text, n
            assert "run_fail" not in text and "run_pass" not in text, n
    side = json.loads(bundle.sidecar_path.read_text(encoding="utf-8"))
    assert side["project_id"] == PROJECT_ID and side["takes"][0]["run_id"] == "run_fail"
    assert bundle.sidecar_path.parent == bundle.zip_path.parent
    assert not bundle.sidecar_path.name.endswith(".zip")


def test_zip_size_is_reported(rig: Rig) -> None:
    bundle = build_three(rig)
    assert bundle.size_bytes == bundle.zip_path.stat().st_size > 10_000
    side = json.loads(bundle.sidecar_path.read_text(encoding="utf-8"))
    assert side["size_bytes"] == bundle.size_bytes and side["zip"] == bundle.zip_path.name
    assert side["zip_blake3"] == bundle.zip_blake3
    seq, head = Ledger(rig.project.root).head()
    assert side["ledger_head"] == {"seq": seq, "hash": head} and seq == 3
    assert rb.export_review(rig.project, ["run_fail"], rig.out).suffix == ".zip"


def test_export_review_returns_the_zip_path_and_is_deterministic(rig: Rig) -> None:
    rig.add_run("run_a", report=failing_report())
    fixed = lambda: datetime(2026, 10, 17, 9, 41, 7, tzinfo=UTC)  # noqa: E731
    first = rb.export_review(rig.project, ["run_a"], rig.out, "REVIEW", 2048, clock=fixed)
    raw = first.read_bytes()
    second = rb.export_review(rig.project, ["run_a"], rig.out, clock=fixed)
    assert first == second and second.read_bytes() == raw  # same inputs and clock: same bytes
    assert rb.export_review(rig.project, ["run_a"], rig.out) == first  # the id and name never depend on time
    other = rb.export_review(rig.project, ["run_a"], rig.out, "PREVIEW")
    assert other != first
    assert list(rig.out.glob(".build-*")) == [] and not list(rig.out.glob("*.tmp"))


# ----------------------------------------------------------------------------- the proxies


def test_watermark_is_tiled_at_twelve_percent() -> None:
    src = gradient(1200, 700)
    plain = rb.make_proxy(src, watermark=None)
    marked = rb.make_proxy(src, watermark="REVIEW")
    diff = np.abs(marked - plain)
    assert diff.max() <= rb.WATERMARK_OPACITY + 1 / 255
    assert diff.max() > 0.05
    changed = (diff.max(axis=-1) > 1 / 255).mean()
    assert 0.01 < changed < 0.5
    h, w = diff.shape[:2]
    for ys, xs in (
        (slice(0, h // 2), slice(0, w // 2)),
        (slice(0, h // 2), slice(w // 2, w)),
        (slice(h // 2, h), slice(0, w // 2)),
        (slice(h // 2, h), slice(w // 2, w)),
    ):
        assert (diff[ys, xs].max(axis=-1) > 0.02).any(), "the watermark must tile across the whole frame"
    # it reads on white and on black: both extremes still change
    white = rb.make_proxy(np.ones((300, 500, 3), np.float32), watermark="REVIEW")
    black = rb.make_proxy(np.zeros((300, 500, 3), np.float32), watermark="REVIEW")
    assert white.min() < 1.0 and black.max() > 0.0


def test_watermark_text_is_validated() -> None:
    img = gradient(100, 60)
    for bad in ("", "   ", "REVIEW é", "x" * 41, None):
        with pytest.raises(ValueError):
            rb.make_proxy(img, watermark=bad) if bad is None and False else rb.check_watermark(bad)  # type: ignore[arg-type]


def test_a_bundle_cannot_be_built_without_a_watermark(rig: Rig) -> None:
    rig.add_run("run_a")
    with pytest.raises(ValueError):
        rb.build_review(rig.project, ["run_a"], rig.out, watermark="")


def test_proxies_are_srgb_tagged_and_keep_the_candidate_lineage(rig: Rig) -> None:
    rig.add_run("run_a", size=(1000, 560))
    b = rb.build_review(rig.project, ["run_a"], rig.out)
    dest = rig.tmp / "unzipped"
    open_zip(b).extractall(dest)
    take_dir = dest / f"review-{b.bundle_id}" / "takes"
    lin = L.read(take_dir / "r1_b.png")
    assert lin is not None and (lin.run_id, lin.engine_id, lin.packet_id) == (
        "run_a",
        "nano-banana-2.1",
        "pkt_room_a",
    )
    assert L.read(take_dir / "r1_a.png") is None  # the plate had none
    raw = (take_dir / "r1_b.png").read_bytes()
    assert b"iCCP" in raw[:200], "sRGB profile tag expected before the pixel data"
    assert raw.index(b"iTXt") < raw.index(b"IDAT")
    # the pixels are the candidate's pixels, watermark apart: no 16-bit master, no resample surprise
    got = imageio.read(take_dir / "r1_b.png").rgb
    want = gradient(1000, 560, 0.6)
    assert np.median(np.abs(got - want)) < 2 / 255


def test_lineage_can_be_left_out(rig: Rig) -> None:
    rig.add_run("run_a", size=(600, 340))
    b = rb.build_review(rig.project, ["run_a"], rig.out, keep_lineage=False)
    dest = rig.tmp / "u2"
    open_zip(b).extractall(dest)
    assert L.read(dest / f"review-{b.bundle_id}" / "takes" / "r1_b.png") is None
    assert PROJECT_ID.encode() not in (dest / f"review-{b.bundle_id}" / "takes" / "r1_b.png").read_bytes()


def test_a_linear_exr_candidate_goes_through_the_srgb_transfer(rig: Rig) -> None:
    import OpenEXR

    lin = np.full((90, 160, 3), 0.18, np.float32)
    path = rig.tmp / "cand.exr"
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage},
        {"RGB": lin.astype(np.float16)},
    ).write(str(path))
    shown = rb.load_display(path)
    assert shown.shape == (90, 160, 3)
    assert abs(float(shown[0, 0, 0]) - 0.4614) < 0.01  # 18% grey is about 46% in sRGB


def test_the_beauty_exr_of_a_packet_is_read_through_its_manifest() -> None:
    packet = REPO / "fixtures" / "packets" / "room_a"
    if not (packet / "manifest.json").is_file():
        pytest.skip("fixtures/packets/room_a is not built")
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    shown = rb.load_display(packet / "beauty.exr", manifest)
    assert shown.shape == (360, 640, 3) and 0.0 <= shown.min() and shown.max() <= 1.0 and shown.mean() > 0.02
    tif = rb.load_display(packet / "display.tif")
    assert tif.shape == (360, 640, 3)
    # the EXR re-derivation and the signed-off TIFF agree closely (decision 17 gates this properly)
    assert np.abs(shown - tif).mean() < 0.02


# ----------------------------------------------------------------------------- finding a run's files


def test_plate_comes_from_a_registered_packet_and_overscan_is_cropped(rig: Rig) -> None:
    packet = REPO / "fixtures" / "packets" / "room_a"
    if not (packet / "manifest.json").is_file():
        pytest.skip("fixtures/packets/room_a is not built")
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    rig.project.register_packet("pkt_room_a", {"path": str(packet), "manifest_blake3": "00" * 32})
    cand = rig.tmp / "cand.png"
    imageio.write_png16(cand, gradient(640, 268))  # the deliverable 2.39:1 frame inside the 16:9 plate
    ck = rig.cas.put_file(cand, "png")
    rig.ledger.append(kind="done", run_id="run_pk", data={"result_key": ck, "packet_id": "pkt_room_a"})
    src = rb.resolve_take(rig.project, "run_pk")
    assert src.plate == (packet / "display.tif").resolve() and src.plate_manifest is not None
    assert manifest["camera"]["safe_frame"]["height"] == 268
    b = rb.build_review(rig.project, ["run_pk"], rig.out)
    assert b.takes[0].size_mismatch is False and (b.takes[0].width, b.takes[0].height) == (640, 268)
    zf = open_zip(b)
    assert png_header(zf.read(f"review-{b.bundle_id}/takes/r1_a.png"))[:2] == (640, 268)


def test_size_mismatch_is_flagged_not_hidden(rig: Rig) -> None:
    rig.add_run("run_odd", size=(800, 450), plate_size=(800, 500))
    b = rb.build_review(rig.project, ["run_odd"], rig.out)
    assert b.takes[0].size_mismatch is True
    assert page_data(open_zip(b))["takes"][0]["size_mismatch"] is True


def test_runs_that_cannot_be_reviewed_are_refused(rig: Rig) -> None:
    with pytest.raises(rb.ReviewError, match="not in this project's ledger"):
        rb.build_review(rig.project, ["run_ghost"], rig.out)
    rig.ledger.append(kind="failed", run_id="run_dead", reason="vendor refused")
    with pytest.raises(rb.ReviewError, match="no result"):
        rb.build_review(rig.project, ["run_dead"], rig.out)
    rig.ledger.append(kind="done", run_id="run_noplate", data={"result_key": "ab" * 32})
    with pytest.raises(rb.ReviewError, match="not in the project CAS"):
        rb.build_review(rig.project, ["run_noplate"], rig.out)
    for bad in ([], "run_a"):
        with pytest.raises(ValueError):
            rb.build_review(rig.project, bad, rig.out)  # type: ignore[arg-type]
    assert not list(rig.out.glob("*.zip")) and not list(rig.out.glob(".build-*"))


def test_a_video_take_is_refused(rig: Rig) -> None:
    clip = rig.tmp / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    plate = rig.tmp / "p.png"
    imageio.write_png16(plate, gradient(80, 60))
    src = rb.TakeSource("run_v", plate, clip)
    rig.ledger.append(kind="done", run_id="run_v")
    with pytest.raises(rb.ReviewError, match="stills only"):
        rb.build_review(rig.project, ["run_v"], rig.out, sources={"run_v": src})


def test_explicit_sources_and_captions(rig: Rig) -> None:
    cand, plate = rig.tmp / "c.png", rig.tmp / "p.png"
    imageio.write_png16(cand, gradient(300, 200, 1.0))
    imageio.write_png16(plate, gradient(300, 200))
    rig.ledger.append(kind="done", run_id="run_x")
    src = rb.TakeSource("run_x", plate, cand, caption="Lobby, west wall")
    b = rb.build_review(rig.project, ["run_x"], rig.out, sources={"run_x": src})
    assert page_data(open_zip(b))["takes"][0]["name"] == "Lobby, west wall"


def test_duplicate_run_ids_make_one_take(rig: Rig) -> None:
    rig.add_run("run_a", size=(400, 300))
    b = rb.build_review(rig.project, ["run_a", "run_a"], rig.out)
    assert [t.label for t in b.takes] == ["r1"]


def test_an_nda_project_still_builds_a_local_bundle(tmp_path: Path) -> None:
    rig = Rig(tmp_path, nda=True)
    rig.add_run("run_a", size=(500, 300))
    b = rb.build_review(rig.project, ["run_a"], rig.out)
    assert json.loads(b.sidecar_path.read_text(encoding="utf-8"))["nda"] is True


# ----------------------------------------------------------------------------- the page


def index_text(rig: Rig, **kw) -> str:
    rig.add_run("run_a", size=(500, 300), report=failing_report())
    b = rb.build_review(rig.project, ["run_a"], rig.out, **kw)
    zf = open_zip(b)
    return zf.read(f"review-{b.bundle_id}/index.html").decode("utf-8")


def test_page_has_the_controls(rig: Rig) -> None:
    text = index_text(rig, title="Clubhouse <stills> & more", reply_to="studio@example.com")
    tags = Tags()
    tags.feed(text)
    for needed in (
        "img-a",
        "img-b",
        "wipe",
        "btn-toggle",
        "btn-zoom",
        "mail",
        "download",
        "reviewer",
        "take-note",
        "overall",
        "gate",
        "takes",
        "plate-review-data",
    ):
        assert needed in tags.ids, needed
    assert tags.ids["wipe"]["type"] == "range"
    assert tags.ids["mail"]["href"] == "mailto:"
    assert "clip-path" in text and "mailto:" in text and "Blob" in text and "download" in text
    assert "<stills>" not in text and "&lt;stills&gt;" in text  # the title is escaped in the HTML
    data = json.loads(
        re.search(r'id="plate-review-data" type="application/json">(.*?)</script>', text, re.S).group(1)
    )
    assert data["title"] == "Clubhouse <stills> & more" and data["reply_to"] == "studio@example.com"
    assert "<" not in re.search(
        r'id="plate-review-data" type="application/json">(.*?)</script>', text, re.S
    ).group(1)


def test_page_makes_no_network_call_and_holds_no_credential(rig: Rig) -> None:
    text = index_text(rig)
    low = text.lower()
    for banned in (
        "http://",
        "https://",
        "//cdn",
        "fetch(",
        "xmlhttprequest",
        "websocket",
        "sendbeacon",
        "eventsource",
        "importscripts",
        "@import",
        "<link",
        "<iframe",
        "<form action",
        'src="http',
    ):
        assert banned not in low, banned
    for secret in ("bearer", "token", "api_key", "apikey", "secret", "password", "authorization"):
        assert secret not in low, secret
    tags = Tags()
    tags.feed(text)
    srcs = [d.get("src") for tag, d in tags.tags if tag in ("script", "img", "iframe") and d.get("src")]
    assert srcs == []  # everything is inline; the images are set from the data blob
    csp = next(d["content"] for tag, d in tags.tags if tag == "meta" and d.get("http-equiv"))
    assert "default-src 'none'" in csp and "connect-src" not in csp  # connections fall back to none


def test_reply_to_is_validated_and_lands_in_readme(rig: Rig) -> None:
    rig.add_run("run_a", size=(300, 200))
    for bad in ("not an address", "a@b@c.com", "x@y.com?bcc=z@evil.com", "a b@c.com", "<x@y.com>"):
        with pytest.raises(ValueError):
            rb.build_review(rig.project, ["run_a"], rig.out, reply_to=bad)
    b = rb.build_review(rig.project, ["run_a"], rig.out, reply_to="studio@example.com", title="Stills")
    readme = open_zip(b).read(f"review-{b.bundle_id}/README.txt").decode("ascii")
    assert "studio@example.com" in readme and "REVIEW" in readme and "{{" not in readme
    assert "Send by email" in readme and "comments.json" in readme


def test_the_exporter_itself_has_no_network_import() -> None:
    src = (Path(rb.__file__)).read_text(encoding="utf-8")
    for banned in ("import httpx", "import requests", "urllib.request", "import socket", "boto3"):
        assert banned not in src
    template = "".join(
        p.read_text(encoding="utf-8")
        for p in rb.TEMPLATE_DIR.iterdir()
        if p.suffix in (".html", ".js", ".css")
    )
    assert "fetch(" not in template and "XMLHttpRequest" not in template
    assert (rb.TEMPLATE_DIR / "HOSTING.md").is_file()
