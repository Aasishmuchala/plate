"""index.py rebuild, wave 8 (T63): overrides and accepts replayed from the ledger, phash table filled."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from plate_daemon.index import Index, replay_ledger_notes
from plate_daemon.ledger import Ledger
from plate_daemon.phash import PhashIndex, resolve_drop
from plate_daemon.projects import Project
from plate_daemon.routes.runs import run_view

PROJECT = "wave8"
PACKET = "pkt_room_a"
ROUTE = {"engine_id": "nano-banana-2.1", "provider": "google", "region": "global"}
OVERRIDE_REASON = "glazing reflection is intended; client signed off in the review"
NOTE_KINDS = ("override", "accept")


def picture(seed: int = 3, size: tuple[int, int] = (640, 360)) -> Image.Image:
    """A structured test picture (gradient, boxes, a little noise): perceptual hashes need structure."""
    rng = np.random.default_rng(seed)
    w, h = size
    y = np.linspace(0, 1, h)[:, None, None]
    x = np.linspace(0, 1, w)[None, :, None]
    base = np.concatenate(
        [
            y * rng.random() + x * rng.random(),
            (1 - y) * rng.random() + x * 0.3,
            y * 0.4 + (1 - x) * rng.random(),
        ],
        axis=2,
    )
    img = Image.fromarray((np.clip(base, 0, 1) * 200 + 20).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for _ in range(9):
        x0, y0 = int(rng.integers(0, w - 80)), int(rng.integers(0, h - 60))
        box = [x0, y0, x0 + int(rng.integers(40, 260)), y0 + int(rng.integers(30, 180))]
        fill = tuple(int(v) for v in rng.integers(0, 255, 3))
        (draw.rectangle if rng.random() < 0.5 else draw.ellipse)(box, fill=fill)
    noisy = np.asarray(img).astype(np.float32) + rng.normal(0, 4, (h, w, 3))
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _record(run_id: str, kind: str, state: str, node_id: str, **extra) -> dict:
    """A ledger record in the shape `Pipeline.ledger` writes: state and packet under `data`."""
    data = {"state": state, "packet_id": PACKET, **extra.pop("data", {})}
    return {
        "kind": kind,
        "run_id": run_id,
        "node_id": node_id,
        "engine_id": ROUTE["engine_id"],
        "route_id": ROUTE["provider"],
        **extra,
        "data": data,
    }


def _run_history(ledger: Ledger, run_id: str, node_id: str, final: str, result_key: str) -> None:
    """quoted -> approved -> submitted -> done|gated, as the pipeline ledgers a run."""
    ledger.append(
        _record(
            run_id,
            "quoted",
            "QUOTED",
            node_id,
            cost_usd_quoted=0.04,
            data={
                "priority": "interactive",
                "inputs": {},
                "params": {},
                "route": ROUTE,
                "attempt": 1,
                "quote": {"engine_id": ROUTE["engine_id"], "price_usd": 0.04},
            },
        )
    )
    ledger.append(_record(run_id, "approved", "APPROVED", node_id, by="human", cost_usd_quoted=0.04))
    ledger.append(_record(run_id, "submitted", "SUBMITTED", node_id, provider_job_id="job-" + run_id))
    kind = "done" if final == "DONE" else "gated"
    ledger.append(
        _record(
            run_id,
            kind,
            final,
            node_id,
            provider_job_id="job-" + run_id,
            data={"outputs": [result_key], "result_key": result_key},
        )
    )


def _world(tmp_path: Path) -> tuple[Project, dict[str, str]]:
    """A project folder with one packet, two runs (an overridden GATE_FAIL, an accepted DONE) and a stored PNG."""
    project = Project.create(tmp_path / "nas" / PROJECT, PROJECT, {"nda": False})
    key = project.cas().put(png_bytes(picture()), "png")
    project.register_packet(
        PACKET,
        {
            "shot_id": "room_a",
            "kind": "still",
            "tier": "A",
            "packet_version": "0.1.1",
            "path": str(tmp_path / "packets" / PACKET),
            "manifest_blake3": "a" * 64,
        },
    )
    ledger = Ledger(project.root)
    _run_history(ledger, "run_over", "node-over", "GATE_FAIL", key)
    # The override route and an inline export override both ledger the same decision; a custody edit also
    # arrives as an `override` record and is not a run override.
    over = _record("run_over", "override", "GATE_FAIL", "node-over", by="human", reason=OVERRIDE_REASON)
    ledger.append(over)
    ledger.append(over)
    ledger.append(
        _record(
            "run_over",
            "override",
            "GATE_FAIL",
            "node-over",
            by="human",
            reason="custody moved to C1 for the client",
            data={"what": "custody"},
        )
    )
    _run_history(ledger, "run_ok", "node-ok", "DONE", key)
    ledger.append(
        _record(
            "run_ok",
            "scrub",
            "DONE",
            "node-ok",
            opaque_id="scrub-7",
            data={"scrub_log_id": "scrub-7", "full_scrub": True},
        )
    )
    # accept_route: ledger(pid, "done", run, by=body.by, data={"scrub_log_id": ..., "decision": "accepted"})
    ledger.append(
        _record(
            "run_ok",
            "done",
            "DONE",
            "node-ok",
            by="human",
            data={"scrub_log_id": "scrub-7", "decision": "accepted"},
        )
    )
    return project, {"png": key}


def _notes(idx: Index) -> list[tuple[str, str, str, str]]:
    with idx.project(PROJECT) as conn:
        rows = conn.execute(
            "SELECT run_id, kind, actor, reason FROM run_events WHERE kind IN (?, ?) ORDER BY run_id, seq",
            NOTE_KINDS,
        ).fetchall()
    return [tuple(r) for r in rows]


def test_rebuild_replays_overrides_and_accepts_from_the_ledger(tmp_path, settings):
    project, _ = _world(tmp_path)
    idx = Index(settings.home)
    report = idx.rebuild_from_project(project.root)
    assert report["problems"] == []
    assert report["runs"] == 2 and report["notes_replayed"] == 2

    with idx.project(PROJECT) as conn:
        over = run_view(conn, "run_over")
        ok = run_view(conn, "run_ok")
    assert over["state"] == "GATE_FAIL"
    assert over["override"]["reason"] == OVERRIDE_REASON  # the custody edit did not replace it
    assert over["override"]["by"] == "human"
    assert over["accepted"] is None
    assert over["exportable"] is True
    assert ok["state"] == "DONE"
    assert ok["accepted"]["scrub_log_id"] == "scrub-7"
    assert ok["accepted"]["decision"] == "accepted"
    assert ok["accepted"]["by"] == "human"
    assert ok["override"] is None
    assert ok["exportable"] is True

    # One note per decision although the ledger holds the override twice.
    assert _notes(idx) == [
        (
            "run_ok",
            "accept",
            "human",
            json.dumps({"decision": "accepted", "scrub_log_id": "scrub-7"}, sort_keys=True),
        ),
        ("run_over", "override", "human", "override: " + OVERRIDE_REASON),
    ]


def test_rebuilding_twice_adds_no_duplicate_notes(tmp_path, settings):
    project, _ = _world(tmp_path)
    idx = Index(settings.home)
    idx.rebuild_from_project(project.root)
    first = _notes(idx)
    idx.rebuild_from_project(project.root)
    assert _notes(idx) == first

    # The replay itself is idempotent over an index that kept its notes.
    records, _ = project.ledger_records()
    with idx.project(PROJECT) as conn:
        assert replay_ledger_notes(conn, records) == (0, [])
    assert _notes(idx) == first

    # A fresh machine's index gets the same notes.
    other = Index(tmp_path / "other-machine")
    other.rebuild_from_project(project.root)
    with other.project(PROJECT) as conn:
        rows = conn.execute(
            "SELECT run_id, kind, actor, reason FROM run_events WHERE kind IN (?, ?) ORDER BY run_id, seq",
            NOTE_KINDS,
        ).fetchall()
    assert [tuple(r) for r in rows] == first


def test_only_accepted_decisions_and_known_runs_are_replayed(tmp_path, settings):
    project, _ = _world(tmp_path)
    ledger = Ledger(project.root)
    ledger.append(
        _record("run_over", "done", "GATE_FAIL", "node-over", by="human", data={"decision": "rerolled"})
    )
    ledger.append(
        _record("run_over", "done", "GATE_FAIL", "node-over", by="agent", data={"decision": "accepted"})
    )
    idx = Index(settings.home)
    report = idx.rebuild_from_project(project.root)
    assert report["problems"] == []
    assert report["notes_replayed"] == 3  # override, accept (run_ok), and the bare accept on run_over
    with idx.project(PROJECT) as conn:
        over = run_view(conn, "run_over")
    # the note carries what the ledger holds; whether it unlocks anything is `find_scrub`'s call
    assert over["accepted"]["scrub_log_id"] is None and over["accepted"]["by"] == "agent"

    # A decision about a run the folder cannot back is skipped (the fold reports the run itself).
    gone = Ledger(project.root)
    gone.append(
        _record("run_gone", "override", "GATE_FAIL", "node-x", by="human", reason="a run with no packet")
    )
    again = Index(tmp_path / "again").rebuild_from_project(project.root)
    assert again["notes_replayed"] == 3
    assert all("run_gone" in p for p in again["problems"]) and again["problems"] != []


def test_reindex_fills_the_phash_table_and_drop_to_resolve_finds_a_derivative(tmp_path, settings):
    project, keys = _world(tmp_path)
    cas = project.cas()
    junk = cas.put(b"this is not an image", "png")  # a CAS object is whatever bytes were put there
    raw = cas.put(b"exr-ish bytes", "exr")  # not a phash extension
    idx = Index(settings.home)
    report = idx.rebuild_from_project(project.root)
    assert report["phashed"] >= 1
    assert report["phashed"] == 1 and report["phash_skipped"] == 1
    assert report["problems"] == []  # an unreadable image is skipped, not a problem

    with idx.project(PROJECT) as conn:
        phashes = {r["hash"]: r["phash"] for r in conn.execute("SELECT hash, phash FROM assets")}
    assert phashes[keys["png"]] is not None and len(phashes[keys["png"]]) == 64
    assert phashes[junk] is None and phashes[raw] is None

    # A stripped, smaller, lossy derivative of the stored PNG: no lineage, not the same bytes.
    small = picture().resize((384, 216), Image.Resampling.BICUBIC)
    jpeg = tmp_path / "screenshot.jpg"
    small.save(jpeg, quality=70)
    hit = resolve_drop(jpeg, PhashIndex(idx, PROJECT))
    assert hit is not None
    assert hit["by"] == "phash"
    assert hit["content_hash"] == keys["png"]

    # A fresh machine gets the same table.
    other = Index(tmp_path / "other-machine")
    assert other.rebuild_from_project(project.root)["phashed"] == 1
    again = resolve_drop(jpeg, PhashIndex(other, PROJECT))
    assert again is not None and again["by"] == "phash" and again["content_hash"] == keys["png"]


def test_verify_skips_a_corrupt_asset_and_does_not_hash_it(tmp_path, settings):
    project, keys = _world(tmp_path)
    path = project.cas().find(keys["png"])
    assert path is not None
    path.write_bytes(png_bytes(picture(seed=9)))  # a valid PNG, but not the bytes its name promises
    idx = Index(settings.home)
    report = idx.rebuild_from_project(project.root, verify=True)
    assert any("do not match the hash" in p for p in report["problems"])
    assert report["phashed"] == 0
    with idx.project(PROJECT) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE hash = ?", (keys["png"],)).fetchone()[0] == 0
