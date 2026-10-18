#!/usr/bin/env python3
"""The scale/soak attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T128/T133-T135).

    python scripts/stress/scale.py [--keep] [--only <check>] [--list]

The attacker's scale scripts (autosave.py, graphtest.py, shapes_http.py, scale_packet.py, ledger_fill.py,
ledger_flow.py), turned around:

- autosave-storm: 150 overlapping PUTs of a 500-node, 600-edge graph, one every 400 ms without waiting, are each
  answered 200, 409 graph_busy or 429 too_many_saves (never a 500 or project_locked), and /health stays under 3 s.
- deep-graph: a chain of 200 Colour nodes saves in under 2 s.
- ingest-unlisted-ids: a packet whose Cryptomatte holds thousands of tiny ids the legend does not list ingests in
  under 10 s. The attacker measured 60 ms per unlisted id at 3840x2160 (400 ids, 28 s); the standard library cannot
  write a 4K multi-layer EXR in reasonable time, so the check rewrites two ZIP blocks of the committed 640x360
  room_a EXR with UNLISTED_IDS one-pixel ids, the same ids-times-pixels work. The cold quote of that packet is
  recorded, not asserted (the 4K figure needs a 4K packet).
- ledger-reread: with 50,000 records in runs.jsonl an approval answers in under 100 ms (median of ten). The records
  are written by a helper process with the blake3 package, chained exactly as the daemon chains them.
"""

from __future__ import annotations

import itertools
import json
import operator
import statistics
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, error_of, note

STORM_PUTS = 150
STORM_EVERY_S = 0.4
UNLISTED_IDS = 16000
LEDGER_RECORDS = 50_000
_SHARED: Daemon | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="scale-shared").start()
    return _SHARED


def storm_graph(packet_id: str, mask_id: str) -> dict[str, Any]:
    """graphtest.py's 8 plates x 20 Mask -> Edit -> Export branches, plus 40 Edit -> Edit edges: 500 nodes, 600 edges."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for p in range(8):
        pn = f"plate{p}"
        nodes.append({"id": pn, "kind": "Plate", "label": "Plate", "packetId": packet_id, "tier": "A"})
        for k in range(20):
            m, e, x = f"p{p}m{k}", f"p{p}e{k}", f"p{p}x{k}"
            nodes.append({"id": m, "kind": "Mask", "label": "Mask", "params": {"ids": [mask_id]}})
            nodes.append(
                {
                    "id": e,
                    "kind": "Edit",
                    "label": "Edit",
                    "params": {"engine": "fake", "prompt": f"edit {p}-{k}"},
                }
            )
            nodes.append({"id": x, "kind": "Export", "label": "Export"})
            edges += [
                {"id": f"{pn}->{m}", "source": pn, "target": m},
                {"id": f"{m}->{e}", "source": m, "target": e},
                {"id": f"{e}->{x}", "source": e, "target": x},
            ]
        for k in range(10):
            edges.append(
                {"id": f"p{p}e{k}->p{p}e{k + 10}", "source": f"p{p}e{k}", "target": f"p{p}e{k + 10}"}
            )
        for k in range(5):
            t = (k + 1) % 10 + 10
            edges.append({"id": f"p{p}e{k}->p{p}e{t}x", "source": f"p{p}e{k}", "target": f"p{p}e{t}"})
    for i in range(12):
        nodes.append({"id": f"frame{i}", "kind": "Frame", "label": "Frame"})
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------- an EXR with unlisted ids (stdlib)

_MINUS128 = bytes((b - 128) & 255 for b in range(256))
_PLUS128 = bytes((b + 128) & 255 for b in range(256))


def _zip_decode(packed: bytes, size: int) -> bytearray:
    """One EXR ZIP block: inflate, undo the delta predictor, then the even/odd byte split."""
    raw = zlib.decompress(packed) if len(packed) < size else packed
    if len(packed) >= size:
        return bytearray(raw)
    delta = raw[:1] + raw[1:].translate(_MINUS128)
    flat = bytes(map((255).__and__, itertools.accumulate(delta)))
    half = (len(flat) + 1) // 2
    out = bytearray(len(flat))
    out[0::2], out[1::2] = flat[:half], flat[half:]
    return out


def _zip_encode(data: bytes) -> bytes:
    split = bytes(data[0::2]) + bytes(data[1::2])
    diff = bytes(map((255).__and__, map(operator.sub, split[1:], split[:-1])))
    packed = zlib.compress(split[:1] + diff.translate(_PLUS128), 6)
    return packed if len(packed) < len(data) else bytes(data)


def with_unlisted_ids(src: Path, dest: Path, count: int) -> int:
    """Copy a single-part ZIP scanline EXR, giving `count` pixels of CryptoObject00 rank 0 a fresh id with
    coverage 1 (ids no legend lists), rewriting only the blocks they fall in. Returns how many were placed."""
    data = src.read_bytes()
    magic, version = struct.unpack_from("<ii", data, 0)
    if magic != 20000630 or version & 0x1200:
        raise ValueError(f"{src.name}: not a single-part scanline EXR")
    pos, attrs = 8, {}
    while data[pos] != 0:
        name_end = data.index(b"\0", pos)
        type_end = data.index(b"\0", name_end + 1)
        size = struct.unpack_from("<i", data, type_end + 1)[0]
        attrs[data[pos:name_end].decode()] = data[type_end + 5 : type_end + 5 + size]
        pos = type_end + 5 + size
    header_end = pos + 1
    if attrs["compression"] != b"\x03":
        raise ValueError(f"{src.name}: ZIP compression expected")
    xmin, ymin, xmax, ymax = struct.unpack("<4i", attrs["dataWindow"])
    width, height = xmax - xmin + 1, ymax - ymin + 1
    channels, cl = [], attrs["channels"]
    i = 0
    while cl[i] != 0:
        end = cl.index(b"\0", i)
        ptype = struct.unpack_from("<i", cl, end + 1)[0]
        channels.append((cl[i:end].decode(), 2 if ptype == 1 else 4))
        i = end + 1 + 16
    lines_per_block = 16
    nblocks = (height + lines_per_block - 1) // lines_per_block
    offsets = struct.unpack_from(f"<{nblocks}Q", data, header_end)
    line_bytes = sum(width * s for _, s in channels)
    col = {}
    acc = 0
    for name, s in channels:
        col[name] = (acc, s)
        acc += width * s
    rows_needed = (count + width - 1) // width
    blocks: list[bytes] = []
    placed = 0
    for off in offsets:
        y, packed_size = struct.unpack_from("<ii", data, off)
        packed = data[off + 8 : off + 8 + packed_size]
        lines = min(lines_per_block, height - (y - ymin))
        if (y - ymin) >= rows_needed:
            blocks.append(struct.pack("<ii", y, packed_size) + packed)
            continue
        raw = _zip_decode(packed, lines * line_bytes)
        rid, rs = col["CryptoObject00.R"]
        cov, _ = col["CryptoObject00.G"]
        for ln in range(lines):
            base = ln * line_bytes
            for x in range(width):
                if placed >= count:
                    break
                bits = 0x3E000000 + placed * 13 + 1  # a finite, normal float32, unique per id
                raw[base + rid + x * rs : base + rid + (x + 1) * rs] = struct.pack("<I", bits)
                raw[base + cov + x * 4 : base + cov + (x + 1) * 4] = struct.pack("<f", 1.0)
                placed += 1
        enc = _zip_encode(bytes(raw))
        blocks.append(struct.pack("<ii", y, len(enc)) + enc)
    table_start = header_end
    first = table_start + 8 * nblocks
    new_offsets, at = [], first
    for blk in blocks:
        new_offsets.append(at)
        at += len(blk)
    dest.write_bytes(data[:header_end] + struct.pack(f"<{nblocks}Q", *new_offsets) + b"".join(blocks))
    return placed


def unlisted_packet(d: Daemon, project: str, count: int) -> Path:
    """room_a with `count` unlisted one-pixel Cryptomatte ids, a fresh packet id and rehashed files."""
    dest = d.stage_packet(project, name=f"room_a_{project}")
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    placed = with_unlisted_ids(_lib.FIXTURES / "room_a" / "beauty.exr", dest / "beauty.exr", count)
    if placed != count:
        raise RuntimeError(f"placed {placed} of {count} ids")
    exr = (dest / "beauty.exr").read_bytes()
    entry = m["files"]["beauty_exr"]
    entry["bytes"], entry["hash"]["hex"] = len(exr), d.blake3(exr)
    (dest / "manifest.json").write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8", newline="\n")
    return dest


# ---------------------------------------------------------------------------- a 50,000-record ledger (helper)

FILL_LEDGER = r"""
import json, sys
from blake3 import blake3
path, n_records, packet_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]
ORDER = ("v", "seq", "prev_hash", "hash", "at", "kind", "run_id", "node_id", "engine_id", "route_id",
         "custody_grade", "cost_usd_quoted", "cost_usd_billed", "provider_job_id", "opaque_id", "gate_report", "by",
         "reason", "data")
def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
with open(path, "rb") as fh:
    lines = [ln for ln in fh.read().splitlines() if ln.strip()]
last = json.loads(lines[-1]) if lines else None
seq, prev = (last["seq"], last["hash"]) if last else (0, "0" * 64)
kinds = ("quoted", "approved", "submitted", "fetched", "done", "export")
out = []
i = 0
while len(out) < n_records:
    run = "run_FILL%018d" % i
    for k, kind in enumerate(kinds):
        if len(out) >= n_records:
            break
        seq += 1
        body = {"v": 1, "seq": seq, "prev_hash": prev, "at": "2026-01-01T00:%02d:%02d.000000Z" % ((i // 60) % 60, i % 60),
                "kind": kind, "run_id": run, "node_id": "edit", "engine_id": "fake", "route_id": "fake-local",
                "custody_grade": "C0", "data": {"state": kind.upper(), "packet_id": packet_id, "fill": True}}
        if kind == "approved":
            body["by"] = "human"
        h = blake3((prev + canon(body)).encode("utf-8")).hexdigest()
        rec = {key: (h if key == "hash" else body[key]) for key in ORDER if key == "hash" or key in body}
        out.append(json.dumps(rec, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
        prev = h
    i += 1
with open(path, "ab") as fh:
    fh.write(("\n".join(out) + "\n").encode("ascii"))
print(seq, flush=True)
"""


# ---------------------------------------------------------------------------- checks


def autosave_storm() -> None:
    d = shared()
    p = "storm"
    pkt, mask = d.edit_project(p)
    g = storm_graph(pkt, mask)
    first = d.api("PUT", f"/projects/{p}/graph", g, ui=True)
    if not check(
        first.status_code == 200, f"the 600-edge graph does not save: {first.status_code} {first.text[:200]}"
    ):
        return
    results: list[tuple[float, Any]] = []
    lock = threading.Lock()
    health: list[float] = []
    stop = threading.Event()

    def monitor() -> None:
        while not stop.is_set():
            health.append(d.health_ms(30))
            time.sleep(0.25)

    def one(i: int) -> None:
        body = json.loads(json.dumps(g))
        body["nodes"][1]["label"] = f"edit {i}"  # a drag or a rename changes the document a little
        body["nodes"][0]["position"] = {"x": i, "y": i}
        c = d.make_client(ui=True, timeout=180)
        t0 = time.monotonic()
        try:
            r: Any = c.put(f"/projects/{p}/graph", json=body)
        except Exception as e:
            r = e
        finally:
            c.close()
        with lock:
            results.append((time.monotonic() - t0, r))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    threads = []
    t_start = time.monotonic()
    for i in range(STORM_PUTS):
        t = threading.Thread(target=one, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(max(0.0, t_start + (i + 1) * STORM_EVERY_S - time.monotonic()))
    for t in threads:
        t.join(240)
    stop.set()
    mon.join(35)
    kinds = Counter(
        (_lib.status_of(r), error_of(r) if hasattr(r, "status_code") else None) for _, r in results
    )
    allowed = {(200, None), (409, "graph_busy"), (429, "too_many_saves")}
    check(set(kinds) <= allowed, f"answers to {STORM_PUTS} overlapping PUTs: {dict(kinds)}")
    check(kinds.get((200, None), 0) >= 1, f"no PUT of the storm saved: {dict(kinds)}")
    worst = max(health) if health else float("inf")
    check(worst < 3000, f"/health took up to {worst:.0f} ms during the storm")
    lat = sorted(x for x, _ in results)
    if lat:
        note(
            f"{dict(kinds)}; PUT p50 {lat[len(lat) // 2]:.2f} s, max {lat[-1]:.2f} s; /health max {worst:.0f} ms"
        )


def deep_graph() -> None:
    d = shared()
    p = "deep"
    pkt, _ = d.edit_project(p)
    nodes = [{"id": "plate", "kind": "Plate", "label": "Plate", "packetId": pkt, "tier": "A"}]
    edges, prev = [], "plate"
    for i in range(199):
        nid = f"c{i}"
        nodes.append({"id": nid, "kind": "Colour", "label": "Colour"})
        edges.append({"id": f"{prev}->{nid}", "source": prev, "target": nid})
        prev = nid
    t0 = time.monotonic()
    r = d.api("PUT", f"/projects/{p}/graph", {"nodes": nodes, "edges": edges}, ui=True)
    dt = time.monotonic() - t0
    check(r.status_code in (200, 422), f"PUT chain-200 -> {r.status_code} {r.text[:160]}")
    check(dt < 2.0, f"PUT of a 200-node chain took {dt:.2f} s")
    note(f"chain-200 PUT {dt:.2f} s ({r.status_code})")


def ingest_unlisted_ids() -> None:
    d = shared()
    p = "slivers"
    assert d.project(p).status_code == 201
    path = unlisted_packet(d, p, UNLISTED_IDS)
    t0 = time.monotonic()
    rec = d.post_packet(path, timeout_s=300)
    dt = time.monotonic() - t0
    check(rec.get("status") == "ingested", f"the packet ended {rec.get('status')}")
    check(dt < 10.0, f"ingest of a packet with {UNLISTED_IDS} unlisted ids took {dt:.1f} s")
    pid = rec["packet_id"]
    g = _lib.edit_graph(pid, d.mask_id(pid))
    d.api("PUT", f"/projects/{p}/graph", g, ui=True)
    t1 = time.monotonic()
    q = d.quote(p)
    note(f"ingest {dt:.1f} s; cold quote {time.monotonic() - t1:.2f} s at 640x360 ({q.status_code})")


def ledger_reread() -> None:
    d = shared()
    p = "ledger50k"
    d.edit_project(p)
    d.done_run(p)  # a real run first, so the file holds the daemon's own records too
    led = d.home / "projects" / p / "runs.jsonl"
    pkt = d.api("GET", f"/projects/{p}/packets").json()[0]["packet_id"]
    d.stop()
    done = subprocess.run(
        [sys.executable, "-c", FILL_LEDGER, str(led), str(LEDGER_RECORDS), pkt],
        env=_lib.child_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if not check(done.returncode == 0, f"the ledger fill failed: {done.stderr[-300:]}"):
        d.start()
        return
    d.start()
    n = sum(1 for _ in led.open("rb"))
    check(n >= LEDGER_RECORDS, f"runs.jsonl holds {n} records")
    c = d.make_client()
    times, quotes = [], []
    try:
        for i in range(11):
            t0 = time.monotonic()
            q = c.post(f"/projects/{p}/nodes/edit/quote", json={"engine_id": "fake", "prompt": f"l{i}"})
            quotes.append(time.monotonic() - t0)
            rid = q.json()["run"]["id"]
            t0 = time.monotonic()
            a = c.post(f"/runs/{rid}/approve", json={"by": "human", "start": False})
            times.append(time.monotonic() - t0)
            check(a.status_code == 200, f"approve -> {a.status_code} {a.text[:120]}")
    finally:
        c.close()
    med = statistics.median(times[1:]) * 1000
    check(med < 100, f"approve on a {n}-record ledger: median {med:.0f} ms")
    note(
        f"{n} records: approve median {med:.0f} ms, quote median {statistics.median(quotes[1:]) * 1000:.0f} ms"
    )


CHECKS = {
    "autosave-storm": autosave_storm,
    "deep-graph": deep_graph,
    "ingest-unlisted-ids": ingest_unlisted_ids,
    "ledger-reread": ledger_reread,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
