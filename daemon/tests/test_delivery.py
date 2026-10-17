"""Delivery (router plan T46): FCPXML 1.10, ProRes 4444, EXR sequence, HEVC with colour tags, AV1 re-tag.

ffmpeg and ffprobe are used for real on tiny synthetic frames (no client data); every encoder-dependent test
skips when the tool or the encoder is absent. The colour tests decode the file with an EXPLICIT matrix and
range (not the file's own tags) so they measure what the encoder did to the pixels, not what it wrote on the
label; a wrong-matrix decode must disagree, which proves the check can fail.
"""

from __future__ import annotations

import json
import re
import subprocess
import types
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import numpy as np
import pytest

from plate_daemon import lineage
from plate_daemon.delivery import fcpxml, frames, guard, tools, video
from plate_daemon.delivery.guard import (
    DeliveryError,
    DeliveryRefused,
    Provenance,
    authorize,
    sidecar_path,
)
from plate_daemon.lineage import Lineage

W, H = 128, 72
RGB = (200, 60, 30)  # saturated enough that a BT.601/709 mix-up is far outside tolerance


# ---------------------------------------------------------------------------- helpers


def _lineage(**kw) -> Lineage:
    base = {
        "project_id": "proj-1",
        "node_id": "node-7",
        "run_id": "run-42",
        "packet_id": "room_a",
        "engine_id": "nano-banana-2.1",
        "engine_version": "2.1.0",
        "gate_report": "gates/run-42.1.json",
    }
    base.update(kw)
    return Lineage(**base)


def _report(verdict: str = "pass") -> dict:
    return {
        "gate_version": "0.1.0",
        "metric_code_hash": "ab" * 32,
        "threshold_set_id": "v1",
        "verdict": verdict,
        "checks": [{"metric_id": "texture.lapvar", "value": 412.0, "status": verdict}],
    }


def _prov(**kw) -> Provenance:
    kw.setdefault("gate_report", _report())
    kw.setdefault("report_ref", "gates/run-42.1.json")
    return Provenance(lineage=_lineage(), **kw)


def _flat(rgb=RGB, n=6, alpha=None) -> list[np.ndarray]:
    """n identical uint8 frames of one colour, optionally RGBA with the left half transparent."""
    a = np.zeros((H, W, 3 if alpha is None else 4), np.uint8)
    a[..., :3] = rgb
    if alpha is not None:
        a[..., 3] = 255
        a[:, : W // 2, 3] = alpha
    return [a.copy() for _ in range(n)]


def _need_ffmpeg(*encoders: str) -> None:
    try:
        tools.ffmpeg_path()
        tools.ffprobe_path()
    except guard.ToolMissing:
        pytest.skip("ffmpeg / ffprobe not installed")
    listing = subprocess.run(
        [tools.ffmpeg_path(), "-hide_banner", "-encoders"], capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")
    for enc in encoders:
        if not re.search(rf"\s{re.escape(enc)}\s", listing):
            pytest.skip(f"ffmpeg has no {enc} encoder")


def _decode_first(path: Path, *, matrix: str = "bt709", rng: str = "tv", fmt: str = "rgb24") -> np.ndarray:
    """First frame as (H, W, C) uint8, decoded with an explicit YUV->RGB matrix and range."""
    cmd = [
        tools.ffmpeg_path(),
        "-v",
        "error",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale=in_color_matrix={matrix}:in_range={rng}:out_range=pc,format={fmt}",
        "-f",
        "rawvideo",
        "-",
    ]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    c = 4 if fmt == "rgba" else 3
    return np.frombuffer(out, np.uint8).reshape(H, W, c)


def _md5_of_decode(path: Path) -> str | None:
    proc = subprocess.run(
        [tools.ffmpeg_path(), "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "md5", "-"],
        capture_output=True,
        check=False,
    )
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def _no_stage_leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.startswith(".")]


# ---------------------------------------------------------------------------- guard


def test_previs_is_refused_with_no_override_path():
    prov = _prov(previs=True, override_reason="the client asked for it nicely")
    with pytest.raises(DeliveryRefused) as e:
        authorize(prov)
    assert e.value.code == "PREVIS_NOT_DELIVERABLE"


def test_gate_fail_needs_an_override_with_a_real_reason_and_logs_it():
    heard: list[str] = []
    with pytest.raises(DeliveryRefused) as e:
        authorize(_prov(run_state="GATE_FAIL"))
    assert e.value.code == "GATE_FAIL"
    with pytest.raises(DeliveryRefused) as e:
        authorize(_prov(run_state="GATE_FAIL", override_reason="   too short "))
    assert e.value.code == "OVERRIDE_REASON_TOO_SHORT"
    assert heard == []
    ok = authorize(
        _prov(
            run_state="GATE_FAIL",
            override_reason="  client approved the crunchy bark  ",
            on_override=heard.append,
        )
    )
    assert (
        ok is not None and ok["reason"] == "client approved the crunchy bark" and ok["at_utc"].endswith("Z")
    )
    assert heard == ["client approved the crunchy bark"]  # logged before any byte is written


def test_a_failing_report_on_a_done_run_still_needs_the_override():
    with pytest.raises(DeliveryRefused) as e:
        authorize(_prov(gate_report=_report("fail")))
    assert e.value.code == "GATE_FAIL"


def test_unfinished_runs_and_missing_reports_are_refused():
    with pytest.raises(DeliveryRefused) as e:
        authorize(_prov(run_state="RUNNING"))
    assert e.value.code == "RUN_NOT_DELIVERABLE"
    with pytest.raises(DeliveryRefused) as e:
        authorize(_prov(gate_report=None))
    assert e.value.code == "NO_GATE_REPORT"
    assert authorize(_prov(gate_report=None), require_report=False) is None


def test_provenance_accepts_a_gate_report_object_and_run_enum_states():
    from plate_gates.report import Check, GateReport

    rep = GateReport(checks=[Check(metric_id="texture.lapvar", value=412.0, status="pass")])
    from plate_daemon.states import RunState

    prov = Provenance(lineage=_lineage(), gate_report=rep, run_state=RunState.DONE)
    assert prov.verdict == "pass" and prov.run_state == "DONE" and not prov.failed
    assert prov.gate_report["metric_code_hash"] == rep.metric_code_hash


def test_from_run_logs_the_override_on_the_run_exactly_like_export(settings):
    from plate_daemon.db import tx
    from plate_daemon.index import Index, apply_packet_entry
    from plate_daemon.machine import create_run, get_run, run_events
    from plate_daemon.models import Route, RunCreate

    route = Route(engine_id="nano-banana-2.1", provider="google", region="global")
    with Index(settings.home).project("p9") as conn:
        with tx(conn):
            apply_packet_entry(
                conn,
                "p9",
                "pkt",
                {
                    "shot_id": "s01",
                    "kind": "still",
                    "tier": "A",
                    "packet_version": "0.1.1",
                    "path": str(settings.home / "packets" / "pkt"),
                    "manifest_blake3": "a" * 64,
                },
            )
        run = create_run(conn, RunCreate(project_id="p9", packet_id="pkt", node_id="n1", route=route))
        with tx(conn):
            conn.execute(
                "UPDATE runs SET state = 'GATE_FAIL', provider_job_id = 'job-1' WHERE id = ?", (run.id,)
            )
        run = get_run(conn, run.id)
        prov = Provenance.from_run(
            run, gate_report=_report("fail"), override_reason="accepted by the director on review", conn=conn
        )
        assert prov.lineage.run_id == run.id and prov.lineage.engine_id == "nano-banana-2.1"
        assert prov.run_state == "GATE_FAIL" and not prov.previs
        authorize(prov)
        notes = [ev for ev in run_events(conn, run.id) if ev.kind == "override"]
        assert len(notes) == 1 and notes[0].reason == "override: accepted by the director on review"
        flagged = types.SimpleNamespace(**{**run.__dict__, "params": {"previs": True}})
        assert Provenance.from_run(flagged).previs is True


def test_sidecar_names():
    assert sidecar_path(Path("a") / "shot.mov").name == "shot.mov.gate.json"
    assert sidecar_path(Path("a") / "shot.%04d.exr").name == "shot.gate.json"
    assert sidecar_path(Path("a") / "plate_%06d.exr").name == "plate.gate.json"


def test_fps_snaps_to_exact_rationals():
    assert tools.to_fraction(23.976) == Fraction(24000, 1001)
    assert tools.to_fraction("29.97") == Fraction(30000, 1001)
    assert tools.to_fraction(24) == 24 and tools.to_fraction(25.0) == 25
    assert tools.to_fraction("60000/1001") == Fraction(60000, 1001)
    with pytest.raises(DeliveryError):
        tools.to_fraction(0)


# ---------------------------------------------------------------------------- FCPXML


def _timeline(tmp_path: Path, fps=Fraction(24000, 1001)) -> dict:
    d = tmp_path / "Bar & Grill"
    return {
        "name": "Amphi & plaza <reel>",
        "fps": fps,
        "width": 3840,
        "height": 2160,
        "clips": [
            {"path": d / "A&B master.mov", "start_s": 0, "duration_s": 1, "alpha_path": d / "people #1.mov"},
            {"path": d / "shot2.mov", "start_s": 1, "duration_s": 2, "alpha_path": d / "people #2.mov"},
        ],
    }


def test_fcpxml_parses_escapes_ampersands_and_builds_file_urls(tmp_path):
    out = fcpxml.write(_timeline(tmp_path), tmp_path / "reel.fcpxml")
    raw = out.read_text(encoding="utf-8")
    assert raw.startswith(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10">'
    )
    assert "A&amp;B master" in raw and "Amphi &amp; plaza &lt;reel&gt;" in raw
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", raw)  # no bare ampersand anywhere
    root = ET.fromstring(raw)
    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    srcs = [r.get("src") for r in root.iter("media-rep")]
    assert len(srcs) == 4 and all(s.startswith("file:///") for s in srcs)
    assert "%26" in srcs[0] and "%23" in "".join(srcs) and " " not in "".join(srcs)
    decoded = Path(url2pathname(unquote(urlparse(srcs[0]).path)))
    assert decoded == (tmp_path / "Bar & Grill" / "A&B master.mov").absolute()
    names = [a.get("name") for a in root.iter("asset")]
    assert "A&B master" in names and "people #1" in names  # the parsed text round-trips


def test_fcpxml_ntsc_time_is_exact_and_frame_aligned(tmp_path):
    root = ET.fromstring(fcpxml.build(_timeline(tmp_path)))
    fmt = root.find("./resources/format")
    assert fmt.get("frameDuration") == "1001/24000s"
    assert fmt.get("colorSpace") == "1-1-1 (Rec. 709)" and (fmt.get("width"), fmt.get("height")) == (
        "3840",
        "2160",
    )
    seq = root.find("./library/event/project/sequence")
    assert seq.get("tcFormat") == "NDF" and seq.get("tcStart") == "0s"
    n, d = seq.get("duration")[:-1].split("/")
    assert Fraction(int(n), int(d)) == Fraction(72 * 1001, 24000)  # 3 s of 23.976 is exactly 72 frames
    # float input snaps to the same rational; integer rates stay plain
    f = ET.fromstring(fcpxml.build({**_timeline(tmp_path), "fps": 23.976}))
    assert f.find("./resources/format").get("frameDuration") == "1001/24000s"
    assert (
        ET.fromstring(fcpxml.build({**_timeline(tmp_path), "fps": 25}))
        .find("./resources/format")
        .get("frameDuration")
        == "1/25s"
    )
    assert (
        ET.fromstring(fcpxml.build({**_timeline(tmp_path), "fps": 29.97}))
        .find("./resources/format")
        .get("frameDuration")
        == "1001/30000s"
    )


def test_fcpxml_alpha_is_a_lane_one_connected_clip_with_parent_relative_offset(tmp_path):
    root = ET.fromstring(fcpxml.build(_timeline(tmp_path)))
    spine = root.find("./library/event/project/sequence/spine")
    masters = list(spine)
    assert [m.tag for m in masters] == ["asset-clip", "asset-clip"]
    assets = {a.get("id"): a for a in root.iter("asset")}
    for m in masters:
        (alpha,) = list(m)
        assert alpha.tag == "asset-clip" and alpha.get("lane") == "1"
        assert alpha.get("ref") != m.get("ref") and alpha.get("ref") in assets
        assert alpha.get("duration") == m.get("duration")
        # offsets of connected clips count from the PARENT's start; STAFFAGE used the sequence cursor,
        # which pushed every second shot's alpha past the end of its master
        assert alpha.get("offset") == "0s"
    assert masters[0].get("offset") == "0s" and masters[1].get("offset") == "1001/1000s"  # 24 frames in


def test_fcpxml_gaps_connected_lanes_and_errors(tmp_path):
    tl = _timeline(tmp_path, fps=24)
    tl["clips"] = [
        {"path": tmp_path / "a.mov", "start_s": 0, "duration_s": 1},
        {"path": tmp_path / "b.mov", "start_s": 2, "duration_s": 1},  # one second of gap before it
        {"path": tmp_path / "titles.mov", "start_s": 2.5, "duration_s": 0.5, "lane": 2},
    ]
    root = ET.fromstring(fcpxml.build(tl))
    spine = list(root.find("./library/event/project/sequence/spine"))
    assert [e.tag for e in spine] == ["asset-clip", "gap", "asset-clip"]
    assert spine[1].get("offset") == "1s" and spine[1].get("duration") == "1s"
    (titles,) = list(spine[2])
    assert titles.get("lane") == "2" and titles.get("offset") == "1/2s"  # 12 frames into its parent
    tl["clips"][1]["start_s"] = 0.5  # backwards
    with pytest.raises(fcpxml.FcpxmlError, match="before the previous clip ends"):
        fcpxml.build(tl)
    tl["clips"][1]["start_s"] = 2
    tl["clips"][2]["start_s"] = 9  # attaches to nothing
    with pytest.raises(fcpxml.FcpxmlError, match="outside every spine clip"):
        fcpxml.build(tl)
    with pytest.raises(fcpxml.FcpxmlError, match="no clips"):
        fcpxml.build({**tl, "clips": []})


def test_fcpxml_validator_catches_a_broken_document(tmp_path):
    good = fcpxml.build(_timeline(tmp_path))
    fcpxml.validate(good)
    for bad, needle in (
        (good.replace('ref="r3"', 'ref="r99"', 1), "not in <resources>"),
        (good.replace('tcFormat="NDF"', 'tcFormat="DF"'), "NDF"),
        (good.replace("1001/24000s", "1001/23999s", 1), "whole number of frames"),
        (good.replace('lane="1"', 'lane="0"', 1), "non-zero integer lane"),
        (good.replace("file://", "http://", 1), "file:// src"),
        (good.replace("<fcpxml", "<fcpxml2", 1).replace("</fcpxml>", "</fcpxml2>"), "root must be"),
        (good[: len(good) // 2], "well-formed"),
    ):
        with pytest.raises(fcpxml.FcpxmlError, match=re.escape(needle)):
            fcpxml.validate(bad)


def test_fcpxml_write_never_overwrites_and_writes_a_sidecar_with_provenance(tmp_path):
    out = tmp_path / "reel.fcpxml"
    fcpxml.write(_timeline(tmp_path), out, provenance=_prov(gate_report=None))
    side = json.loads(sidecar_path(out).read_text(encoding="utf-8"))
    assert side["format"] == "fcpxml" and side["lineage"]["run_id"] == "run-42"
    assert side["files"][0]["name"] == "reel.fcpxml" and len(side["files"][0]["blake3"]) == 64
    with pytest.raises(FileExistsError):
        fcpxml.write(_timeline(tmp_path), out)
    with pytest.raises(DeliveryError, match=r"\.fcpxml"):
        fcpxml.write(_timeline(tmp_path), tmp_path / "reel.xml")
    with pytest.raises(DeliveryRefused):
        fcpxml.write(_timeline(tmp_path), tmp_path / "x.fcpxml", provenance=_prov(previs=True))
    assert not (tmp_path / "x.fcpxml").exists()


def test_fcpxml_refuses_media_whose_sidecar_says_previs_or_unresolved_fail(tmp_path):
    from plate_daemon.fsutil import write_json_atomic

    tl = _timeline(tmp_path)
    previs_media = tl["clips"][0]["path"]
    write_json_atomic(sidecar_path(previs_media), {"previs": True, "gate": {"verdict": "pass"}})
    with pytest.raises(DeliveryRefused) as e:
        fcpxml.write(tl, tmp_path / "a.fcpxml")
    assert e.value.code == "PREVIS_NOT_DELIVERABLE" and not (tmp_path / "a.fcpxml").exists()
    write_json_atomic(
        sidecar_path(previs_media), {"previs": False, "run_state": "GATE_FAIL", "override": None}
    )
    with pytest.raises(DeliveryRefused) as e:
        fcpxml.write(tl, tmp_path / "b.fcpxml")
    assert e.value.code == "GATE_FAIL"
    write_json_atomic(
        sidecar_path(previs_media),
        {"previs": False, "run_state": "GATE_FAIL", "override": {"reason": "approved on review"}},
    )
    assert fcpxml.write(tl, tmp_path / "c.fcpxml").is_file()


# ---------------------------------------------------------------------------- ProRes 4444


def test_prores4444_tags_alpha_lineage_and_sidecar(tmp_path):
    _need_ffmpeg("prores_ks")
    out = tmp_path / "out" / "people.mov"
    prov = _prov()
    done = video.prores4444(_flat(alpha=0), out, 24000 / 1001, provenance=prov)
    assert done.path == out and done.format == "prores4444" and done.override is None
    s = tools.probe_video(out)
    assert (s["codec_name"], s["profile"], s["codec_tag_string"]) == ("prores", "4444", "ap4h")
    assert s["pix_fmt"] in video._PRORES_PIX_FMTS  # ffmpeg decodes a 4444 stream as 12-bit
    assert (s["color_primaries"], s["color_transfer"], s["color_space"], s["color_range"]) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )
    assert s["fps"] == Fraction(24000, 1001) and int(s["nb_frames"]) == 6
    assert (s["width"], s["height"]) == (W, H)
    rgba = _decode_first(out, fmt="rgba")
    assert rgba[:, : W // 4, 3].max() <= 2 and rgba[:, 3 * W // 4 :, 3].min() >= 253  # alpha survived
    assert lineage.read(out) == prov.lineage
    side = json.loads(sidecar_path(out).read_text(encoding="utf-8"))
    assert side["gate"]["verdict"] == "pass" and side["gate"]["report"]["threshold_set_id"] == "v1"
    assert side["lineage"]["run_id"] == "run-42" and side["override"] is None and side["previs"] is False
    assert side["color"]["verified"] is True and side["color"]["color_space"] == "bt709"
    assert side["files"] == [
        {"name": "people.mov", "bytes": out.stat().st_size, "blake3": guard.blake3_file(out)}
    ]
    assert str(tmp_path) not in sidecar_path(out).read_text(encoding="utf-8")  # names, never absolute paths
    assert _no_stage_leftovers(out.parent) == []


def test_prores_uses_the_709_matrix_not_swscales_601_default(tmp_path):
    _need_ffmpeg("prores_ks")
    out = tmp_path / "flat.mov"
    video.prores4444(_flat(n=3), out, 24, provenance=_prov())
    err = np.abs(_decode_first(out).astype(int) - np.array(RGB)).max()
    assert err <= 2, err
    wrong = np.abs(_decode_first(out, matrix="smpte170m").astype(int) - np.array(RGB)).max()
    assert wrong > 5, "the check must be able to tell a BT.601 decode from a BT.709 one"


def test_delivery_refusals_leave_nothing_behind(tmp_path):
    _need_ffmpeg("prores_ks")
    out = tmp_path / "x.mov"
    for prov, code in (
        (_prov(previs=True), "PREVIS_NOT_DELIVERABLE"),
        (_prov(run_state="GATE_FAIL"), "GATE_FAIL"),
        (_prov(gate_report=None), "NO_GATE_REPORT"),
    ):
        with pytest.raises(DeliveryRefused) as e:
            video.prores4444(_flat(), out, 24, provenance=prov)
        assert e.value.code == code
    assert list(tmp_path.iterdir()) == []
    ok = video.prores4444(
        _flat(),
        out,
        24,
        provenance=_prov(
            run_state="GATE_FAIL", gate_report=_report("fail"), override_reason="director signed off"
        ),
    )
    side = json.loads(sidecar_path(ok.path).read_text(encoding="utf-8"))
    assert side["override"]["reason"] == "director signed off" and side["run_state"] == "GATE_FAIL"
    assert side["gate"]["verdict"] == "fail"  # the fail stays on the record
    with pytest.raises(FileExistsError):
        video.prores4444(_flat(), out, 24, provenance=_prov())
    assert _no_stage_leftovers(tmp_path) == []


def test_prores_input_forms_files_pattern_folder_and_video(tmp_path):
    _need_ffmpeg("prores_ks")
    from plate_pixels import imageio

    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(1, 5):
        arr = np.full((H, W, 3), RGB, np.uint16) * 257
        imageio.write_png16(folder / f"f{i:04d}.png", arr)
    a = video.prores4444(folder / "f%04d.png", tmp_path / "pattern.mov", 24, provenance=_prov())
    b = video.prores4444(folder, tmp_path / "folder.mov", 24, provenance=_prov())
    assert int(tools.probe_video(a.path)["nb_frames"]) == int(tools.probe_video(b.path)["nb_frames"]) == 4
    c = video.prores4444(a.path, tmp_path / "again.mov", provenance=_prov())  # video in, fps from the file
    assert tools.probe_video(c.path)["fps"] == Fraction(24)
    with pytest.raises(DeliveryError, match="never retimes"):
        video.prores4444(a.path, tmp_path / "retimed.mov", 25, provenance=_prov())
    with pytest.raises(DeliveryError, match="fps is required"):
        video.prores4444(folder, tmp_path / "nofps.mov", provenance=_prov())
    with pytest.raises(DeliveryError, match="never resizes"):
        video.prores4444(
            [np.zeros((H, W, 3), np.uint8), np.zeros((H + 2, W, 3), np.uint8)],
            tmp_path / "mixed.mov",
            24,
            provenance=_prov(),
        )
    assert not (tmp_path / "mixed.mov").exists() and _no_stage_leftovers(tmp_path) == []


# ---------------------------------------------------------------------------- HEVC


def test_hevc_main10_hvc1_with_tags_in_the_container_and_the_bitstream(tmp_path):
    _need_ffmpeg("libx265")
    out = tmp_path / "review.mp4"
    prov = _prov()
    done = video.hevc(_flat(), out, 24, provenance=prov)
    s = tools.probe_video(out)
    assert (s["codec_name"], s["profile"], s["pix_fmt"], s["codec_tag_string"]) == (
        "hevc",
        "Main 10",
        "yuv420p10le",
        "hvc1",
    )
    assert (s["color_primaries"], s["color_transfer"], s["color_space"], s["color_range"]) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )
    # the VUI inside the bitstream, with the container's own tags thrown away
    raw = tmp_path / "raw.h265"
    subprocess.run(
        [tools.ffmpeg_path(), "-v", "error", "-i", str(out), "-c", "copy", "-f", "hevc", str(raw)], check=True
    )
    rs = tools.probe_video(raw)
    assert (rs["color_primaries"], rs["color_transfer"], rs["color_space"], rs["color_range"]) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )
    assert lineage.read(out) == prov.lineage and done.sidecar.name == "review.mp4.gate.json"
    err = np.abs(_decode_first(out).astype(int) - np.array(RGB)).max()
    assert err <= 3, err
    assert np.abs(_decode_first(out, matrix="smpte170m").astype(int) - np.array(RGB)).max() > 5


def test_hevc_refuses_alpha_it_cannot_carry_but_takes_an_opaque_alpha_plane(tmp_path):
    _need_ffmpeg("libx265")
    with pytest.raises(DeliveryError, match="no alpha channel"):
        video.hevc(_flat(alpha=0), tmp_path / "a.mp4", 24, provenance=_prov())
    assert list(tmp_path.iterdir()) == []
    opaque = video.hevc(_flat(alpha=255), tmp_path / "b.mp4", 24, provenance=_prov())
    assert tools.probe_video(opaque.path)["pix_fmt"] == "yuv420p10le"
    with pytest.raises(DeliveryError, match="unknown colour tags"):
        video.hevc(_flat(), tmp_path / "c.mp4", 24, tags="rec2020", provenance=_prov())


# ---------------------------------------------------------------------------- EXR


def _exr_info(path: Path) -> dict:
    import OpenImageIO as oiio

    inp = oiio.ImageInput.open(str(path))
    spec = inp.spec()
    px = np.asarray(inp.read_image("float")).reshape(spec.height, spec.width, spec.nchannels)
    info = {
        "format": str(spec.format),
        "compression": spec.getattribute("compression"),
        "chromaticities": spec.getattribute("chromaticities"),
        "channels": spec.nchannels,
        "px": px,
    }
    inp.close()
    return info


def test_exr_sequence_is_linear_half_zip_with_rec709_chromaticities(tmp_path):
    ramp = np.tile(np.linspace(0, 1, W, dtype=np.float32), (H, 1))
    imgs = [np.stack([ramp, np.full_like(ramp, 0.5), ramp[::-1]], axis=2) for _ in range(3)]
    prov = _prov()
    done = video.exr_sequence(imgs, tmp_path / "shot.%04d.exr", provenance=prov)
    assert [f.name for f in done.files] == ["shot.0001.exr", "shot.0002.exr", "shot.0003.exr"]
    assert done.sidecar.name == "shot.gate.json"
    info = _exr_info(done.files[0])
    assert info["format"] == "half" and info["compression"] == "zip" and info["channels"] == 3
    assert np.allclose(info["chromaticities"], video.REC709_CHROMATICITIES, atol=1e-5)
    # display 0.5 is scene-linear 0.2140 (the sRGB EOTF), not 0.5
    assert abs(float(info["px"][..., 1].mean()) - 0.21404) < 2e-3
    assert abs(float(info["px"][0, W - 1, 0]) - 1.0) < 1e-3
    for f in done.files:  # lineage went in without disturbing the colour header
        assert lineage.read(f) == prov.lineage
        assert np.allclose(_exr_info(f)["chromaticities"], video.REC709_CHROMATICITIES, atol=1e-5)
    side = json.loads(done.sidecar.read_text(encoding="utf-8"))
    assert side["format"] == "exr_sequence" and side["frames"] == 3 and len(side["files"]) == 3
    assert side["color"]["encoding"] == "linear" and side["color"]["pixel_type"] == "half"
    assert _no_stage_leftovers(tmp_path) == []


def test_exr_from_16bit_png_a_still_and_linear_arrays(tmp_path):
    from plate_pixels import imageio

    png = tmp_path / "master.png"
    imageio.write_png16(png, np.full((H, W, 3), 32768, np.uint16))
    still = video.exr_sequence(png, tmp_path / "still.exr", provenance=_prov())
    assert [f.name for f in still.files] == ["still.exr"] and still.sidecar.name == "still.exr.gate.json"
    assert abs(float(_exr_info(still.path)["px"].mean()) - 0.2140) < 3e-3
    lin = video.exr_sequence(
        [np.full((H, W, 3), 0.18, np.float32)],
        tmp_path / "lin.%03d.exr",
        encoding="linear",
        first_frame=7,
        provenance=_prov(),
    )
    assert lin.path.name == "lin.007.exr"
    assert abs(float(_exr_info(lin.path)["px"].mean()) - 0.18) < 1e-3


def test_exr_refuses_alpha_misnumbered_and_existing_frames_without_partial_output(tmp_path):
    two = [np.zeros((H, W, 3), np.uint8)] * 2
    with pytest.raises(DeliveryError, match="no frame field"):
        video.exr_sequence(two, tmp_path / "flat.exr", provenance=_prov())
    with pytest.raises(DeliveryError, match="non-opaque alpha"):
        video.exr_sequence(_flat(alpha=0, n=2), tmp_path / "a.%02d.exr", provenance=_prov())
    with pytest.raises(DeliveryRefused):
        video.exr_sequence(two, tmp_path / "p.%02d.exr", provenance=_prov(previs=True))
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "s.02.exr").write_bytes(b"not yours")
    with pytest.raises(FileExistsError):
        video.exr_sequence(two, tmp_path / "s.%02d.exr", provenance=_prov())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.02.exr"]  # frame 1 was rolled back
    assert (tmp_path / "s.02.exr").read_bytes() == b"not yours"


def test_open_frames_shapes(tmp_path):
    f = frames.open_frames(np.zeros((3, H, W, 3), np.uint8))
    assert (f.width, f.height, f.count, f.has_alpha) == (W, H, 3, False)
    with pytest.raises(DeliveryError, match="no frames match"):
        frames.open_frames(str(tmp_path / "none_%04d.png"))
    with pytest.raises(DeliveryError, match="no frames to deliver"):
        frames.open_frames([])
    with pytest.raises(DeliveryError, match=r"\(H, W, 3 or 4\)"):
        frames.open_frames(np.zeros((H, W), np.uint8))


# ---------------------------------------------------------------------------- AV1 re-tag


def _av1_encoder() -> list[str]:
    _need_ffmpeg()
    listing = subprocess.run(
        [tools.ffmpeg_path(), "-hide_banner", "-encoders"], capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")
    if re.search(r"\slibaom-av1\s", listing):
        return ["-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "40", "-b:v", "0"]
    if re.search(r"\slibsvtav1\s", listing):
        return ["-c:v", "libsvtav1", "-preset", "10"]
    pytest.skip("no AV1 encoder (libaom-av1 / libsvtav1) in this ffmpeg")


def _colr(path: Path) -> tuple[int, int, int, bool]:
    """(primaries, transfer, matrix, full_range) read straight from the MP4 colr/nclx box."""
    data = path.read_bytes()
    i = data.find(b"colrnclx")
    assert i > 0, "no nclx colr box"
    p, t, m = (int.from_bytes(data[i + 8 + 2 * k : i + 10 + 2 * k], "big") for k in range(3))
    return p, t, m, bool(data[i + 14] & 0x80)


@pytest.fixture
def vantage_clip(tmp_path) -> Path:
    """A tiny AV1 clip dressed like a Vantage 3.3 export: bitstream says unspecified, the container's colr
    box says identity (GBR) matrix, full range, sRGB transfer."""
    enc = _av1_encoder()
    base = tmp_path / "base.mp4"
    raw = tmp_path / "raw.rgb"
    raw.write_bytes(b"".join(np.ascontiguousarray(f).tobytes() for f in _flat(n=8)))
    subprocess.run(
        [
            tools.ffmpeg_path(),
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-video_size",
            f"{W}x{H}",
            "-framerate",
            "24",
            "-i",
            str(raw),
            "-vf",
            "format=yuv420p",
            *enc,
            str(base),
        ],
        check=True,
    )
    vant = tmp_path / "vantage.mp4"
    proc = subprocess.run(
        [
            tools.ffmpeg_path(),
            "-v",
            "error",
            "-y",
            "-i",
            str(base),
            "-c",
            "copy",
            "-bsf:v",
            "av1_metadata=color_primaries=2:transfer_characteristics=2:matrix_coefficients=2:color_range=tv",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "iec61966-2-1",
            "-colorspace",
            "rgb",
            "-color_range",
            "pc",
            str(vant),
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("this ffmpeg cannot write a GBR colr box to build the Vantage-style fixture")
    if _colr(vant)[2:] != (0, True):
        pytest.skip("fixture did not carry the identity/full-range colr box")
    return vant


def test_av1_retag_auto_recognises_the_vantage_signature_and_is_lossless(tmp_path, vantage_clip):
    out = tmp_path / "fixed" / "vantage_fixed.mp4"
    prov = _prov(gate_report=None)
    done = video.av1_retag(vantage_clip, out, provenance=prov)
    s = tools.probe_video(out)
    assert (s["color_primaries"], s["color_transfer"], s["color_space"], s["color_range"]) == (
        "bt709",
        "iec61966-2-1",
        "smpte170m",
        "pc",
    )
    assert _colr(out) == (1, 13, 6, True)  # the box itself, not ffprobe's reading of it
    assert _colr(vantage_clip)[2:] == (0, True)  # the source was left alone
    before, after = _md5_of_decode(vantage_clip), _md5_of_decode(out)
    if before is not None:
        assert before == after  # same decoded pixels: the re-tag is a stream copy
    side = json.loads(done.sidecar.read_text(encoding="utf-8"))
    assert side["color"]["profile"] == "vantage33" and side["color"]["lossless"] is True
    assert side["color"]["was"]["color_space"] == "gbr" and side["gate"]["verdict"] == "not_scored"
    assert lineage.read(out) == prov.lineage and _colr(out) == (1, 13, 6, True)  # lineage remux kept the box


def test_av1_retag_plan_profile_gives_bt709_limited(tmp_path, vantage_clip):
    out = tmp_path / "plan.mp4"
    video.av1_retag(vantage_clip, out, profile="bt709", provenance=_prov(gate_report=None))
    s = tools.probe_video(out)
    assert (s["color_primaries"], s["color_transfer"], s["color_space"], s["color_range"]) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )
    assert _colr(out) == (1, 1, 1, False)  # the container box follows, which a bare bsf copy does not do


def test_av1_retag_rejects_other_codecs_overwrites_and_unknown_profiles(tmp_path, vantage_clip):
    _need_ffmpeg("libx265")
    h = video.hevc(_flat(), tmp_path / "h.mp4", 24, provenance=_prov())
    with pytest.raises(DeliveryError, match="needs av1"):
        video.av1_retag(h.path, tmp_path / "no.mp4", provenance=_prov())
    with pytest.raises(DeliveryError, match="new file"):
        video.av1_retag(vantage_clip, vantage_clip, provenance=_prov())
    with pytest.raises(DeliveryError, match="unknown re-tag profile"):
        video.av1_retag(vantage_clip, tmp_path / "p.mp4", profile="rec2020", provenance=_prov())
    with pytest.raises(DeliveryRefused):
        video.av1_retag(vantage_clip, tmp_path / "q.mp4", provenance=_prov(previs=True))
    assert not (tmp_path / "q.mp4").exists() and _no_stage_leftovers(tmp_path) == []
