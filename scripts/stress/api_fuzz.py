#!/usr/bin/env python3
"""The API-fuzz attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T126-T137).

    python scripts/stress/api_fuzz.py [--keep] [--only <check>] [--list]

Each check is the attacker's repro (scratchpad stress/api-fuzz/repro_all.py) turned around: where the attacker
saw a 500, a 200 or a committed row, the check asserts the typed refusal and the untouched state. One shared
daemon carries most checks, each on its own project; `unattended-clock` gets a daemon no human has touched.
Standard library plus httpx; the daemon is reached over HTTP only (its SQLite index is read, never written).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from typing import Any

import _lib
from _lib import Daemon, check, error_of, is_json, png, skip

_SHARED: Daemon | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="api_fuzz-shared").start()
    return _SHARED


def raw_json(d: Daemon, method: str, path: str, text: str) -> Any:
    """A JSON body httpx would refuse to encode (Infinity, NaN, 1e400): the attacker's curl."""
    return d.client.request(
        method, path, content=text.encode("utf-8"), headers={"Content-Type": "application/json"}
    )


def envelope_rows(d: Daemon, project: str) -> int:
    db = d.home / "index" / f"{project}.sqlite"
    if not db.exists():
        return 0
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        return int(con.execute("SELECT COUNT(*) FROM budget_envelopes").fetchone()[0])
    finally:
        con.close()


def rss_mb(pid: int) -> float | None:
    """Resident set size of a process in MiB (ps on macOS/Linux); None where ps is not there (Windows)."""
    if sys.platform == "win32" or shutil.which("ps") is None:
        return None
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out.isdigit() else None


class PeakRss:
    """Sample a process's RSS on a thread while the body runs; `.delta` is peak minus the starting value."""

    def __init__(self, pid: int) -> None:
        self.pid, self.base, self.peak = pid, rss_mb(pid), 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            v = rss_mb(self.pid)
            if v is not None:
                self.peak = max(self.peak, v)
            time.sleep(0.03)

    def __enter__(self) -> PeakRss:
        if self.base is not None:
            self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._t.is_alive():
            self._t.join(2)

    @property
    def delta(self) -> float | None:
        return None if self.base is None else max(0.0, self.peak - self.base)


# ---------------------------------------------------------------------------- checks


def unattended_clock() -> None:
    """No request says X-Plate-Client: ui; agent spend stops at the 20 USD unattended cap and no header-less
    POST /timer, scrub, verdict or accept lifts it. A UI request does."""
    with Daemon(name="api_fuzz-clock") as d:
        p = "clock"
        assert d.project(p).status_code == 201
        rec = d.post_packet(d.stage_packet(p))
        pid = rec["packet_id"]
        g = _lib.edit_graph(pid, d.mask_id(pid), "nano-banana-pro")
        assert d.api("PUT", f"/projects/{p}/graph", g).status_code == 200  # no ui header: not a human
        # one finished take, approved by `auto` (under the 2 USD auto cap), for the scrub/verdict/accept routes
        take = d.quote_run(p, engine_id="fake", prompt="take")
        a = d.approve(take, by="auto")
        check(a.status_code == 200, f"auto approval of the first take -> {a.status_code} {a.text[:200]}")
        check(d.wait_run(take).get("state") == "DONE", f"the first take did not finish: {d.run(take)}")
        env = d.api(
            "POST",
            "/envelopes",
            {
                "project_id": p,
                "owner": "agent:night",
                "max_total_usd": 1000,
                "max_per_run_usd": 5,
                "ttl_s": 86400,
            },
        ).json()

        def spend(i: int) -> Any:
            q = d.quote(p, engine_id="nano-banana-pro", prompt=f"n{i}")
            rid = q.json()["run"]["id"]
            return d.approve(rid, by=f"agent:{env['id']}", envelope_id=env["id"], start=False)

        spent, refusal = 0, None
        for i in range(150):
            r = spend(i)
            if r.status_code != 200:
                refusal = r
                break
            spent += 1
        if not check(
            refusal is not None and error_of(refusal) == "unattended_cap",
            f"agent approvals never met the unattended cap: {spent} approved, last {refusal and refusal.text[:200]}",
        ):
            return
        sid = d.api("POST", f"/runs/{take}/scrub", {"quadrants_visited": 4, "zoom_100": True}).json()
        lifts = {
            "timer start": lambda: d.api(
                "POST", "/timer", {"deliverable_id": "keepalive", "action": "start"}
            ),
            "timer stop": lambda: d.api("POST", "/timer", {"deliverable_id": "keepalive", "action": "stop"}),
            "timer start (mcp)": lambda: d.api(
                "POST",
                "/timer",
                {"deliverable_id": "k2", "action": "start"},
                headers={"X-Plate-Client": "mcp"},
            ),
            "scrub": lambda: d.api("POST", f"/runs/{take}/scrub", {"quadrants_visited": 4, "zoom_100": True}),
            "verdict": lambda: d.api(
                "POST",
                f"/runs/{take}/verdict",
                {"verdict": "ship", "reasons": [], "scrub_log_id": sid["scrub_log_id"], "rater": "stress"},
            ),
            "accept": lambda: d.api("POST", f"/runs/{take}/accept", {"scrub_log_id": sid["scrub_log_id"]}),
        }
        for what, call in lifts.items():
            r = call()
            check(r.status_code == 200, f"{what} without the ui header -> {r.status_code} {r.text[:200]}")
            after = spend(1000 + len(what))
            check(
                after.status_code == 409 and error_of(after) == "unattended_cap",
                f"after a header-less {what} an agent approval -> {after.status_code} {after.text[:160]}"
                " (the unattended cap was lifted)",
            )
        r = d.api("PUT", f"/projects/{p}/graph", g, ui=True)  # a person at the canvas
        check(r.status_code == 200, f"ui graph PUT -> {r.status_code}")
        after = spend(2000)
        check(
            after.status_code == 200,
            f"after a ui request the agent approval -> {after.status_code} {after.text[:160]}",
        )


def export_race() -> None:
    """Eight concurrent exports of two runs to one name: exactly one first write, the rest 409 or a byte-identical
    repeat of the winner, one file and its sidecar on disk, one non-repeat ledger record. Cutdown and the batch
    route share the claim."""
    d = shared()
    p = "race"
    d.edit_project(p)
    runs = [d.done_run(p, prompt="one"), d.done_run(p, prompt="two")]
    out = d.home / "out_race"

    def export(i: int) -> Any:
        return d.api(
            "POST", f"/runs/{runs[i % 2]}/export", {"out_dir": str(out), "format": "png16", "name": "dup"}
        )

    answers = _lib.concurrently(export, 8)
    codes = Counter(_lib.status_of(r) for r in answers)
    firsts = [r.json() for r in answers if _lib.status_of(r) == 200 and not r.json().get("repeat")]
    repeats = [r.json() for r in answers if _lib.status_of(r) == 200 and r.json().get("repeat")]
    losers = [r for r in answers if _lib.status_of(r) == 409]
    check(set(codes) <= {200, 409}, f"export statuses {dict(codes)}")
    if check(len(firsts) == 1, f"{len(firsts)} first writes of dup.png (want 1): statuses {dict(codes)}"):
        winner = firsts[0]["run_id"]
        check(all(x["run_id"] == winner for x in repeats), "a repeat answered for the run that lost")
    check(all(error_of(r) == "not_exportable" for r in losers), "a 409 that is not not_exportable")
    listing = sorted(x.name for x in out.iterdir()) if out.exists() else []
    check(listing == ["dup.gate.json", "dup.png"], f"out_dir holds {listing}")
    recs = [x for x in d.ledger(p) if x.get("kind") == "export" and not (x.get("data") or {}).get("repeat")]
    check(len(recs) == 1, f"{len(recs)} non-repeat export ledger records for dup.png (want 1)")

    cut = d.home / "out_cut"
    body = {"preset": "1:1", "out_dir": str(cut), "name": "square"}
    answers = _lib.concurrently(lambda i: d.api("POST", f"/runs/{runs[0]}/cutdown", body), 6)
    codes = Counter(_lib.status_of(r) for r in answers)
    check(
        codes.get(200, 0) == 1 and codes.get(409, 0) == 5, f"6 concurrent cutdowns to one name: {dict(codes)}"
    )
    listing = sorted(x.name for x in cut.iterdir()) if cut.exists() else []
    check(listing == ["square.cutdown.json", "square.png"], f"cutdown folder holds {listing}")

    batch = d.home / "out_batch"

    def batch_export(i: int) -> Any:
        body = {"run_ids": [runs[i % 2]], "out_dir": str(batch), "format": "png16", "name_pattern": "shot"}
        return d.api("POST", f"/projects/{p}/export", body)

    answers = _lib.concurrently(batch_export, 6)
    firsts = []
    for r in answers:
        if _lib.status_of(r) != 200:
            check(_lib.status_of(r) in (409, 422), f"batch export -> {_lib.status_of(r)}")
            continue
        firsts += [e for e in r.json().get("exported", []) if not e.get("repeat")]
    check(len(firsts) == 1, f"{len(firsts)} first writes by 6 concurrent batch exports to one name (want 1)")
    listing = sorted(x.name for x in batch.iterdir()) if batch.exists() else []
    check(len([n for n in listing if n.endswith(".png")]) == 1, f"batch folder holds {listing}")


def custody_unset() -> None:
    """`unset` is the server's mark: a client cannot send it, and a re-POST never loosens an NDA project."""
    d = shared()
    r1 = d.api("POST", "/projects", {"project_id": "ndatrap", "custody": {"nda": True, "unset": True}})
    check(r1.status_code == 201, f"create nda+unset -> {r1.status_code} {r1.text[:200]}")
    check("unset" not in (r1.json().get("custody") or {}), f"the client's unset was kept: {r1.text[:200]}")
    r2 = d.api(
        "POST",
        "/projects",
        {"project_id": "ndatrap", "custody": {"nda": False, "allowed_grades": _lib.ALL_GRADES}},
    )
    check(
        r2.status_code == 409 and error_of(r2) == "exists",
        f"plain re-POST loosening the NDA project -> {r2.status_code} {r2.text[:200]}",
    )
    listed = {x["project_id"]: x for x in d.api("GET", "/projects").json()}
    custody = (listed.get("ndatrap") or {}).get("custody") or {}
    check(
        custody.get("nda") is True and set(custody.get("allowed_grades", [])) <= {"C0", "C1"},
        f"custody now {custody}",
    )


def envelope_inf() -> None:
    """Non-finite or absurd caps and TTLs are 422 and commit nothing; the bounds themselves are allowed."""
    d = shared()
    p = "env"
    d.project(p)

    def text(total: str = "1", per_run: str = "1", ttl: str = "3600") -> str:
        return (
            f'{{"project_id": "{p}", "owner": "agent:x", "max_total_usd": {total}, '
            f'"max_per_run_usd": {per_run}, "ttl_s": {ttl}}}'
        )

    cases = [
        ("Infinity", "1", "3600"),
        ("1e400", "1", "3600"),
        ("NaN", "1", "3600"),
        ("1e308", "1", "3600"),
        ("1", "Infinity", "3600"),
        ("1", "1", "Infinity"),
        ("1", "1", "1e30"),
        ("1", "1", "3.15e11"),
    ]
    for total, per_run, ttl in cases:
        r = raw_json(d, "POST", "/envelopes", text(total, per_run, ttl))
        check(
            r.status_code == 422 and is_json(r),
            f"envelope total={total} per_run={per_run} ttl={ttl} -> {r.status_code}",
        )
    check(envelope_rows(d, p) == 0, f"{envelope_rows(d, p)} envelope rows committed by refused requests")
    r = raw_json(d, "POST", "/envelopes", text("1000000", "1000000", "31622400"))
    check(r.status_code == 201, f"the bounds themselves -> {r.status_code} {r.text[:200]}")


def attempt_poison() -> None:
    """An absurd explicit attempt is 422 and leaves the node quotable."""
    d = shared()
    p = "attempt"
    d.edit_project(p)
    for value in (2**63 - 1, 2**63, 10**30):
        r = d.quote(p, attempt=value)
        check(r.status_code == 422 and is_json(r), f"quote attempt={value} -> {r.status_code} {r.text[:160]}")
    r = d.quote(p)
    check(r.status_code == 200, f"a plain quote after the refusals -> {r.status_code} {r.text[:160]}")


def oserror_500() -> None:
    """Uncreatable, too-long or NUL paths answer typed JSON (422 filesystem / bad_path, 507), never a bare 500,
    and nothing reaches the ledger."""
    d = shared()
    p = "oserr"
    pkt, _ = d.edit_project(p)
    rid = d.done_run(p)
    blocker = (
        d.home / "blocker.txt"
    )  # a file where a folder should be: ENOTDIR on POSIX, an OSError on Windows
    blocker.write_text("not a folder\n", encoding="utf-8", newline="\n")
    below = str(blocker / "x")
    posix = sys.platform != "win32"
    etc = "/etc/plate_x" if posix and not os.access("/etc", os.W_OK) else str(blocker / "y")
    devnull = "/dev/null/x" if posix else below
    long_path = str(d.home / ("A" * 300))
    nul = str(d.home / "o") + "\u0000x"
    cases = {
        f"export out_dir={devnull}": ("POST", f"/runs/{rid}/export", {"out_dir": devnull, "format": "png16"}),
        f"export out_dir={etc}": ("POST", f"/runs/{rid}/export", {"out_dir": etc, "format": "png16"}),
        "deliver out_dir under a file": (
            "POST",
            f"/runs/{rid}/deliver",
            {"format": "exr_sequence", "out_dir": below},
        ),
        "cutdown out_dir under a file": ("POST", f"/runs/{rid}/cutdown", {"preset": "1:1", "out_dir": below}),
        "batch export out_dir under a file": (
            "POST",
            f"/projects/{p}/export",
            {"run_ids": [rid], "out_dir": below, "format": "png16"},
        ),
        "POST /projects root unwritable": (
            "POST",
            "/projects",
            {"project_id": "rt1", "custody": {"nda": False}, "root": etc},
        ),
        "POST /packets 300-char component": (
            "POST",
            "/packets",
            {"packet_path": long_path, "sender": _lib.SENDER},
        ),
        "POST /assets/resolve 300-char component": ("POST", "/assets/resolve", {"path": long_path}),
        "deliver out_dir with NUL": (
            "POST",
            f"/runs/{rid}/deliver",
            {"format": "exr_sequence", "out_dir": nul},
        ),
        "import path with NUL": (
            "POST",
            f"/projects/{p}/import",
            {"path": nul, "packet_id": pkt, "source": "manual"},
        ),
        "quote recipe path with NUL": (
            "POST",
            f"/projects/{p}/nodes/edit/quote",
            {"engine_id": "fake", "recipe": nul + ".yaml"},
        ),
    }
    for what, (method, path, body) in cases.items():
        headers = _lib.PACKET_HEADERS if path == "/packets" else None
        r = d.api(method, path, body, headers=headers)
        check(
            r.status_code != 500 and 400 <= r.status_code < 600 and is_json(r),
            f"{what} -> {r.status_code} {r.headers.get('content-type')} {r.text[:160]}",
        )
    exports = [x for x in d.ledger(p) if x.get("kind") == "export"]
    check(not exports, f"{len(exports)} export ledger records after failed writes")


def newline_slugs() -> None:
    """Ids and file names are matched whole: a trailing newline is 422 and nothing is made."""
    d = shared()
    r = d.api("POST", "/projects", {"project_id": "nl1\n", "custody": {"nda": False}})
    check(r.status_code == 422, f"project_id 'nl1\\n' -> {r.status_code}")
    idx = [x.name for x in (d.home / "index").iterdir() if x.name.startswith("nl1")]
    check(not idx, f"index files {idx!r}")
    p = "nl"
    d.edit_project(p)
    rid = d.done_run(p)
    out = d.home / "out_nl"
    r = d.api("POST", f"/runs/{rid}/export", {"out_dir": str(out), "format": "png16", "name": "nlx\n"})
    check(
        r.status_code == 422 and error_of(r) == "bad_name",
        f"export name 'nlx\\n' -> {r.status_code} {r.text[:160]}",
    )
    files = [x.name for x in out.iterdir()] if out.exists() else []
    check(not files, f"files written: {files!r}")
    plate = d.home / "tierc_plate.png"
    plate.write_bytes(png(64, 48))
    before = sorted((d.home / "packets").rglob("tierc-*"))
    r = d.api(
        "POST",
        f"/projects/{p}/tierc",
        {"plate_path": str(plate), "shot_id": "s1\n", "consent_record": "client consent on file 2026-10-01"},
    )
    check(r.status_code == 422, f"tierc shot_id 's1\\n' -> {r.status_code} {r.text[:160]}")
    after = sorted((d.home / "packets").rglob("tierc-*"))
    check(after == before, f"tierc folders made: {[x.name for x in after if x not in before]!r}")


def unbounded_body() -> None:
    """A JSON body over the cap is 413 from its Content-Length, before the body is read; a graph over 4 MiB is
    422 graph_too_large. project.json is untouched."""
    d = shared()
    p = "body"
    d.project(p)
    pj = d.home / "projects" / p / "project.json"
    size = pj.stat().st_size
    with socket.create_connection(("127.0.0.1", d.port), timeout=10) as s:
        head = (
            f"PUT {_lib.API}/projects/{p}/graph HTTP/1.1\r\nHost: 127.0.0.1:{d.port}\r\n"
            f"Authorization: Bearer {d.token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {50 * 1024 * 1024}\r\n\r\n"
        )
        s.sendall(head.encode("ascii"))  # the 50 MiB never follow: the answer must not wait for them
        try:
            first = s.recv(4096).split(b"\r\n", 1)[0]
        except TimeoutError:
            first = b"(no answer in 10 s: the daemon waited for the body)"
    check(b" 413 " in first + b" ", f"PUT /graph declaring 50 MiB -> {first!r}")
    g = {"nodes": [{"id": "c", "kind": "Colour", "label": "x" * (5 * 1024 * 1024)}], "edges": []}
    r = d.api("PUT", f"/projects/{p}/graph", g)
    check(
        r.status_code == 422 and error_of(r) == "graph_too_large",
        f"PUT /graph of 5 MiB -> {r.status_code} {r.text[:120]}",
    )
    check(pj.stat().st_size == size, f"project.json grew from {size} to {pj.stat().st_size} bytes")


def decode_bomb() -> None:
    """A tiny PNG declaring 12000 x 12000 is refused from its header (422 too_many_pixels) on both uploads,
    before any decode."""
    d = shared()
    p = "bomb"
    d.project(p)
    data = png(12000, 12000)
    for kind in ("reference", "mask"):
        with PeakRss(d.pid) as peak:
            r = d.client.post(
                "/assets",
                params={"kind": kind, "project_id": p},
                content=data,
                headers={"Content-Type": "image/png"},
            )
        body = r.json() if is_json(r) else {}
        check(
            r.status_code == 422
            and body.get("error") == "too_many_pixels"
            and body.get("declared") == [12000, 12000],
            f"{kind}: a {len(data) // 1024} KiB PNG declaring 12000x12000 -> {r.status_code} {r.text[:160]}",
        )
        if peak.delta is not None:
            check(peak.delta < 300, f"{kind}: daemon RSS rose {peak.delta:.0f} MiB on a refused upload")
    refs = d.api("GET", f"/projects/{p}/references").json()
    check(refs == [], f"references stored: {refs}")


def pattern_width() -> None:
    """A name_pattern with a format spec, conversion or lookup is 422 bad_name_pattern at once."""
    d = shared()
    p = "pattern"
    d.project(p)
    out = d.home / "o_w"
    for pattern in ("{n:>900000000}", "{n!r}", "{n.real}", "{n[0]}", "{}"):
        with PeakRss(d.pid) as peak:
            t0 = time.monotonic()
            r = d.api(
                "POST",
                f"/projects/{p}/export",
                {"run_ids": ["x"], "out_dir": str(out), "name_pattern": pattern},
            )
            dt = time.monotonic() - t0
        check(
            r.status_code == 422 and error_of(r) == "bad_name_pattern",
            f"name_pattern {pattern!r} -> {r.status_code} {r.text[:160]}",
        )
        check(dt < 2.0, f"name_pattern {pattern!r} took {dt:.2f} s to refuse")
        if peak.delta is not None:
            check(peak.delta < 300, f"name_pattern {pattern!r}: daemon RSS rose {peak.delta:.0f} MiB")
    check(not out.exists(), "the refused batch made its out_dir")


def ghost_index() -> None:
    """An envelope for a project that does not exist is 404 not_found and opens no index."""
    d = shared()
    base = {"owner": "agent:x", "max_total_usd": 1, "max_per_run_usd": 1, "ttl_s": 60}
    r = d.api("POST", "/envelopes", {"project_id": "never-created", **base})
    check(
        r.status_code == 404 and error_of(r) == "not_found",
        f"envelope for a ghost project -> {r.status_code} {r.text[:160]}",
    )
    r2 = d.api("POST", "/envelopes", {"project_id": "never-created-2", **base, "owner": "not-an-owner"})
    check(r2.status_code in (404, 422), f"bad-owner envelope for a ghost project -> {r2.status_code}")
    ghosts = [x.name for x in (d.home / "index").iterdir() if x.name.startswith("never-created")]
    check(not ghosts, f"index files for ghost projects: {ghosts}")
    listed = [x["project_id"] for x in d.api("GET", "/projects").json()]
    check(not any(x.startswith("never-created") for x in listed), f"GET /projects lists {listed}")


def quote_validation() -> None:
    """An unknown priority, a non-finite param and params the engine rejects are 422 at the quote, before any
    run row; a valid count still quotes."""
    d = shared()
    p = "qv"
    d.edit_project(p)
    before = len(d.api("GET", f"/projects/{p}/runs").json())
    cases = [
        ("priority 'high'", d.quote(p, priority="high"), "bad_priority"),
        ("priority 5", d.quote(p, priority=5), "bad_priority"),
        (
            "params.n=Infinity",
            raw_json(
                d,
                "POST",
                f"/projects/{p}/nodes/edit/quote",
                '{"engine_id":"flux-3-image","params":{"n":Infinity}}',
            ),
            "bad_params",
        ),
        (
            "candidate_count=0.5",
            d.quote(p, engine_id="nano-banana-pro", prompt="p", params={"candidate_count": 0.5}),
            "bad_params",
        ),
        (
            "candidate_count=9",
            d.quote(p, engine_id="nano-banana-pro", prompt="p", params={"candidate_count": 9}),
            "bad_params",
        ),
    ]
    for what, r, code in cases:
        # the body model refuses a priority at the door (422 errors); the quote refuses the rest by code
        typed = error_of(r) == code or (code == "bad_priority" and "priority" in str(r.json().get("errors")))
        check(r.status_code == 422 and is_json(r) and typed, f"{what} -> {r.status_code} {r.text[:160]}")
    after = len(d.api("GET", f"/projects/{p}/runs").json())
    check(after == before, f"refused quotes left {after - before} run rows")
    r = d.quote(p, engine_id="nano-banana-pro", prompt="p", params={"candidate_count": 2})
    check(r.status_code == 200, f"candidate_count=2 -> {r.status_code} {r.text[:160]}")


def docs_long_name() -> None:
    d = shared()
    got = {n: d.api("GET", "/docs/" + "A" * n + ".md") for n in (250, 255, 300)}
    got["dir"] = d.api("GET", "/docs/" + "d" * 300 + "/x.md")
    for n, r in got.items():
        check(
            r.status_code == 404 and error_of(r) == "not_found",
            f"GET /docs/<{n}>.md -> {r.status_code} {r.text[:80]}",
        )


def graph_unhashable_id() -> None:
    d = shared()
    p = "gid"
    d.project(p)
    for bad in ({}, []):
        r = d.api("PUT", f"/projects/{p}/graph", {"nodes": [{"id": bad, "kind": "Plate"}], "edges": []})
        codes = [v.get("code") for v in (r.json().get("violations") or [])] if is_json(r) else []
        check(
            r.status_code == 422 and codes == ["UNKNOWN_NODE"],
            f"node id {bad!r} -> {r.status_code} {r.text[:120]}",
        )


def method_404() -> None:
    """HEAD /health is 200; an unknown method or path under /api is the JSON 404."""
    d = shared()
    r = d.client.head("/health")
    check(r.status_code == 200, f"HEAD /health -> {r.status_code}")
    for method, path in (("TRACE", "/health"), ("OPTIONS", "/projects"), ("TRACE", "/x"), ("DELETE", "/x")):
        r = d.client.request(method, path)
        check(
            r.status_code == 404 and error_of(r) == "not_found",
            f"{method} {path} -> {r.status_code} {r.text[:80]}",
        )
    for path in ("/api", "/api/"):
        r = d.client.get(d.base + path)
        check(
            r.status_code == 404 and error_of(r) == "not_found",
            f"GET {path} -> {r.status_code} {r.text[:80]}",
        )


def recipe_yaml_leak() -> None:
    """A recipe path that is not a recipe names the path only; the file's text never comes back."""
    d = shared()
    p = "recipe"
    d.edit_project(p)
    f = d.home / "secret.yaml"
    f.write_text("kind: [unclosed\nsecret: NDA-material-list-line-2\n", encoding="utf-8", newline="\n")
    r = d.quote(p, recipe=str(f))
    check(
        r.status_code == 422 and error_of(r) == "unknown_recipe", f"recipe=<a bad .yaml> -> {r.status_code}"
    )
    check(
        b"unclosed" not in r.content and b"NDA-material" not in r.content,
        f"the error echoes the file: {r.text[:200]}",
    )


def symlink_manifest() -> None:
    """A packet whose manifest.json is a link out of its folder is 422, like a linked data file."""
    d = shared()
    p = "sym"
    d.project(p)
    src = d.stage_packet(p)
    link_dir = d.home / "packets" / "p_sym"
    shutil.copytree(src, link_dir)
    outside = d.home / "man_outside.json"
    outside.write_bytes((link_dir / "manifest.json").read_bytes())
    (link_dir / "manifest.json").unlink()
    # A POSIX check (Windows needs a privilege to make a link at all): `ln -s`, so no Python here depends on one.
    ln = shutil.which("ln")
    if sys.platform == "win32" or ln is None:
        skip("symlinks are a POSIX check (Windows needs admin rights or Developer Mode to make one)")
    made = subprocess.run([ln, "-s", str(outside), str(link_dir / "manifest.json")], capture_output=True)
    if made.returncode != 0:
        skip(f"cannot make a symlink here: {made.stderr.decode('utf-8', errors='replace').strip()}")
    r = d.api(
        "POST", "/packets", {"packet_path": str(link_dir), "sender": _lib.SENDER}, headers=_lib.PACKET_HEADERS
    )
    errors = r.json().get("errors") if is_json(r) else None
    check(
        r.status_code == 422 and errors == ["manifest.json: path escapes the packet folder"],
        f"symlinked manifest -> {r.status_code} {r.text[:200]}",
    )


CHECKS = {
    "unattended-clock": unattended_clock,
    "export-race": export_race,
    "custody-unset": custody_unset,
    "envelope-inf": envelope_inf,
    "attempt-poison": attempt_poison,
    "oserror-500": oserror_500,
    "newline-slugs": newline_slugs,
    "unbounded-body": unbounded_body,
    "decode-bomb": decode_bomb,
    "pattern-width": pattern_width,
    "ghost-index": ghost_index,
    "quote-validation": quote_validation,
    "docs-long-name": docs_long_name,
    "graph-unhashable-id": graph_unhashable_id,
    "method-404": method_404,
    "recipe-yaml-leak": recipe_yaml_leak,
    "symlink-manifest": symlink_manifest,
}

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(_lib.main(CHECKS))
    sys.exit(130)
