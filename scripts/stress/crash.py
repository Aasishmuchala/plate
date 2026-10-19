#!/usr/bin/env python3
"""The crash/resume attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T127-T130,
T138, T144).

    python scripts/stress/crash.py [--keep] [--only <check>] [--list]

The attacker's crash/scripts (crash_matrix, runway_attack, corrupt_attack, corrupt2b, ro_attack, disk_attack,
export_sig, ttl_attack), turned around:

- seedance-negative-prompt: a Seedance 2.5 Motion on the preferred Runway route folds the recipe's negative prompt;
  the approved run reaches the vendor transport (here the recorded-fixture transport, which never touches the
  network: PLATE_LIVE is never set) instead of failing `unknown parameter(s) ['negative_prompt']`.
- fetching-repoll: a run killed with SIGKILL in FETCHING (the echo fake's hang marker) is resumed after the
  restart and reaches DONE, not `result_url_expired`.
- kling-seed-runway: a kling-v3-pro Motion with a seed quotes on Atlas with Runway's skip reason, and pinning the
  Runway route is 422 `params` with no run.
- corrupt-project-json: a truncated, invalid, empty or wrong-schema project.json lists the project as broken and
  answers typed JSON on its routes, never a bare 500.
- index-loss-rebuild: a lost index is rebuilt on boot (runs listed, quotes work), and an SSE cursor from before the
  rebuild replays the new epoch from its first event.
- ro-project-volume: with the project's gates folder read-only (chmod 0o500; no RAM disk needed) an export is 503
  before anything reaches the client folder, and the retry ships and marks the report. POSIX only.
- low-disk: under the free-space floor (`PLATE_MIN_FREE_MB` above the disk's free space: the daemon's own
  setting, so no RAM disk is needed) the run fails `disk_full` before the vendor is called.
- sigkill-export-part: leftovers of an export killed with SIGKILL are swept by the next export of the name.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, error_of, is_json, skip

DUMMY_RUNWAY_KEY = (
    "key_" + "0" * 128
)  # a well-formed key no vendor knows; the fixture transport never sends it
_SHARED: Daemon | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="crash-shared").start()
    return _SHARED


def motion_project(d: Daemon, p: str, params: dict[str, Any]) -> None:
    """Plate -> Mask -> Edit (an accepted, picked take) -> Motion(params), as runway_attack.py built it."""
    d.edit_project(p)
    rid = d.done_run(p)
    sc = d.api("POST", f"/runs/{rid}/scrub", {"quadrants_visited": 4, "zoom_100": True}).json()
    acc = d.api("POST", f"/runs/{rid}/accept", {"scrub_log_id": sc["scrub_log_id"]}, ui=True)
    if acc.status_code != 200:
        raise RuntimeError(f"accept -> {acc.status_code} {acc.text[:200]}")
    g = d.api("GET", f"/projects/{p}/graph").json()
    g = g.get("graph", g)
    nodes = [dict(n) for n in g["nodes"]]
    for n in nodes:
        if n["id"] == "edit":
            n["outputHash"] = d.run(rid)["outputs"][0]
    nodes.append({"id": "motion", "kind": "Motion", "label": "Motion", "params": params})
    edges = [*g["edges"], {"id": "edit->motion", "source": "edit", "target": "motion"}]
    r = d.api("PUT", f"/projects/{p}/graph", {"nodes": nodes, "edges": edges}, ui=True)
    if r.status_code != 200:
        raise RuntimeError(f"PUT motion graph -> {r.status_code} {r.text[:300]}")


def wait_file(path: Path, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


# ---------------------------------------------------------------------------- checks


def seedance_negative_prompt() -> None:
    with Daemon(name="crash-runway", env={"RUNWAYML_API_SECRET": DUMMY_RUNWAY_KEY}) as d:
        p = "rw"
        motion_project(d, p, {"duration": 5, "move": "locked_tripod"})
        q = d.api("POST", f"/projects/{p}/nodes/motion/quote", {"engine_id": "seedance-2.5-i2v"})
        if not check(q.status_code == 200, f"seedance quote -> {q.status_code} {q.text[:200]}"):
            return
        route = q.json()["run"]["route"]
        check(
            route.get("provider") == "runway", f"the seedance quote did not take the preferred route: {route}"
        )
        rid = q.json()["run"]["id"]
        check(d.approve(rid).status_code == 200, "the seedance approval was refused")
        run = d.wait_run(rid, 120)
        err = str(run.get("error") or "")
        check("negative_prompt" not in err, f"the approved run failed on the negative prompt: {err[:240]}")
        check(
            "image_to_video" in err or run.get("state") == "DONE",
            f"the run never reached the vendor transport: {run.get('state')} {err[:240]}",
        )


def fetching_repoll() -> None:
    with Daemon(
        name="crash-fetching", fake={"echo": True, "hang_at": "FETCHING", "hang_phase": "before"}
    ) as d:
        p = "cm"
        d.edit_project(p)
        rid = d.quote_run(p)
        d.approve(rid)
        marker = d.home / "fake_engine.json.hang"
        if not check(wait_file(marker, 60), "the fake never reached its FETCHING hang"):
            return
        check(d.run(rid).get("state") == "FETCHING", f"hung in {d.run(rid).get('state')}")
        d.kill(_lib.sigkill())
        marker.unlink(missing_ok=True)
        d.start(env={"PLATE_FAKE_ENGINE": "1"})
        run = d.wait_run(rid, 120)
        check(
            run.get("state") == "DONE",
            f"after a SIGKILL in FETCHING the run ended {run.get('state')} {run.get('reason_code')} {run.get('error')}",
        )
        jobs = d.journal().get("jobs") or {}
        check(len(jobs) == 1, f"the provider holds {len(jobs)} jobs for one run (a blind resubmit?)")


def kling_seed_runway() -> None:
    d = shared()
    p = "kseed"
    motion_project(d, p, {"duration": 3, "seed": -1})
    q = d.api("POST", f"/projects/{p}/nodes/motion/quote", {"engine_id": "kling-v3-pro"})
    if not check(q.status_code == 200, f"kling quote with a seed -> {q.status_code} {q.text[:200]}"):
        return
    route = q.json()["run"]["route"]
    check(route.get("route_id") == "atlas", f"a seed Runway cannot express quoted on {route.get('route_id')}")
    check(
        any("runway skipped" in r for r in route.get("reasons", [])),
        f"no skip reason: {route.get('reasons')}",
    )
    before = len([r for r in d.api("GET", f"/projects/{p}/runs").json() if r.get("node_id") == "motion"])
    r = d.api(
        "POST", f"/projects/{p}/nodes/motion/quote", {"engine_id": "kling-v3-pro", "route_id": "runway"}
    )
    check(
        r.status_code == 422 and error_of(r) == "params",
        f"pinned runway route -> {r.status_code} {r.text[:200]}",
    )
    after = len([r for r in d.api("GET", f"/projects/{p}/runs").json() if r.get("node_id") == "motion"])
    check(after == before, "the refused pinned quote left a run row")


CORRUPTIONS = {
    "truncate": lambda raw: raw[: len(raw) // 2],
    "invalid": lambda raw: b"{not json at all",
    "empty": lambda raw: b"",
    "schema": lambda raw: json.dumps({**json.loads(raw), "schema": 99}).encode("utf-8"),
}


def corrupt_project_json() -> None:
    d = shared()
    p = "cp"
    pkt, mask = d.edit_project(p)
    quoted = d.quote_run(p)
    pj = d.home / "projects" / p / "project.json"
    original = pj.read_bytes()
    graph = _lib.edit_graph(pkt, mask)
    for mode, corrupt in CORRUPTIONS.items():
        pj.write_bytes(corrupt(original))
        try:
            heads = d.api("GET", "/projects")
            listed = {h["project_id"]: h for h in heads.json()} if heads.status_code == 200 else {}
            check(
                heads.status_code == 200 and listed.get(p, {}).get("broken") is True,
                f"{mode}: GET /projects -> {heads.status_code} {listed.get(p)}",
            )
            calls = {
                "GET graph": d.api("GET", f"/projects/{p}/graph"),
                "GET runs": d.api("GET", f"/projects/{p}/runs"),
                "POST quote": d.quote(p),
                "PUT graph": d.api("PUT", f"/projects/{p}/graph", graph, ui=True),
                "POST /projects": d.project(p),
                "approve": d.approve(quoted),
            }
            for what, r in calls.items():
                ok = (
                    r.status_code < 500 and is_json(r)
                    if what == "GET runs"
                    else 400 <= r.status_code < 500 and is_json(r)
                )
                check(ok, f"{mode}: {what} -> {r.status_code} {r.headers.get('content-type')} {r.text[:120]}")
            g = calls["GET graph"]
            check(error_of(g) == "project_unreadable", f"{mode}: GET graph says {g.text[:120]}")
            check(d.api("GET", "/health").json().get("ok") is True, f"{mode}: /health is not ok")
        finally:
            pj.write_bytes(original)
    check(d.api("GET", f"/projects/{p}/graph").status_code == 200, "the restored project.json does not read")


def stream_ids(d: Daemon, p: str, last: str | None = None) -> list[str]:
    headers = {"Last-Event-ID": last} if last else None
    r = d.client.get("/events/stream", params={"project_id": p, "wait_s": 0}, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"GET /events/stream -> {r.status_code} {r.text[:200]}")
    return [ln[4:].strip() for ln in r.text.splitlines() if ln.startswith("id: ")]


def index_loss_rebuild() -> None:
    d = shared()
    p = "il"
    d.edit_project(p)
    runs = [d.done_run(p, prompt="one"), d.done_run(p, prompt="two")]
    ids = stream_ids(d, p)
    if not check(ids, "the event stream has no ids before the rebuild"):
        return
    last = ids[-1]
    old_epoch = last.rpartition(".")[0]
    d.drop_index(p)
    states = d.run_states(p)
    check(all(states.get(r) == "DONE" for r in runs), f"after a lost index the runs are {states}")
    check(d.api("GET", f"/runs/{runs[0]}").status_code == 200, "GET run after the rebuild failed")
    q = d.quote(p)
    check(q.status_code == 200, f"quote after the rebuild -> {q.status_code} {q.text[:200]}")
    d.done_run(p, prompt="three")
    got = stream_ids(d, p, last)
    epochs = {i.rpartition(".")[0] for i in got}
    seqs = [int(i.rpartition(".")[2]) for i in got if i.rpartition(".")[2].isdigit()]
    check(
        got and old_epoch not in epochs and "" not in epochs,
        f"ids after the rebuild: {got[:5]} (old epoch {old_epoch})",
    )
    check(
        seqs and seqs[0] == 1,
        f"a pre-rebuild cursor resumed at {seqs[:3]} instead of the new epoch's first event",
    )


def ro_project_volume() -> None:
    if sys.platform == "win32":
        skip("a read-only folder mode is a POSIX test (Windows ignores the folder attribute)")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        skip("root ignores the permission bits")
    d = shared()
    p = "ro"
    d.edit_project(p)
    rid = d.done_run(p)
    gates = d.home / "projects" / p / "gates"
    out = d.home / "out_ro"
    mode = gates.stat().st_mode
    os.chmod(gates, 0o500)
    try:
        r = d.api("POST", f"/runs/{rid}/export", {"out_dir": str(out), "format": "png16", "name": "shot"})
        check(
            r.status_code == 503 and "not writable" in str(r.json().get("detail")),
            f"export -> {r.status_code} {r.text[:200]}",
        )
        left = sorted(x.name for x in out.iterdir()) if out.exists() else []
        check(not left, f"files shipped while the project could not record it: {left}")
    finally:
        os.chmod(gates, mode)
    r = d.api("POST", f"/runs/{rid}/export", {"out_dir": str(out), "format": "png16", "name": "shot"})
    check(r.status_code == 200, f"the retry -> {r.status_code} {r.text[:200]}")
    gate = d.api("GET", f"/runs/{rid}/gate").json()
    check(
        gate.get("shipped") is True,
        f"the report is not marked shipped after the retry: {gate.get('shipped')}",
    )


def low_disk() -> None:
    with Daemon(name="crash-disk", env={"PLATE_MIN_FREE_MB": str(10**12)}) as d:
        p = "disk"
        d.edit_project(p)
        rid = d.quote_run(p)
        d.approve(rid)
        run = d.wait_run(rid, 60)
        check(
            run.get("state") == "FAILED" and run.get("reason_code") == "disk_full",
            f"under the free-space floor the run ended {run.get('state')} {run.get('reason_code')}",
        )
        sent = d.journal().get("submit_count", 0)
        check(sent == 0, f"the vendor was called {sent} times on a full disk")
        got = [x["kind"] for x in d.ledger(p) if x.get("run_id") == rid]
        check("submitted" not in got, f"ledger kinds {got}")


def sigkill_export_part() -> None:
    d = shared()
    p = "sig"
    d.edit_project(p)
    rid = d.done_run(p)
    out = d.home / "out_sig"
    out.mkdir(exist_ok=True)
    name = f"{rid}.exr"
    # A real SIGKILL mid-export, at growing delays, until one leaves a hidden temp behind.
    left: list[Path] = []
    for delay in (0.03, 0.06, 0.1, 0.15, 0.25):
        tmp = d.make_client(timeout=5)
        try:
            body = {"out_dir": str(out), "format": "exr"}
            t = threading.Thread(target=_quiet, args=(tmp.post, f"/runs/{rid}/export", body), daemon=True)
            t.start()
            time.sleep(delay)
            d.kill(_lib.sigkill())
            t.join(5)
        finally:
            tmp.close()
        d.start()
        left = [x for x in out.iterdir() if x.name.startswith(".")]
        done = (out / name).exists()
        if left or done:
            break
    planted = out / f".{name}.0123456789ab.exr.part"
    _lib.note(
        f"SIGKILL left {sorted(x.name[:24] for x in left) or 'nothing'}" + ("" if left else ": planted one")
    )
    if not left:  # the kill never landed mid-write on this machine: leave what it would have left
        for f in (out / name, out / f"{rid}.gate.json"):
            f.unlink(missing_ok=True)
        planted.write_bytes(b"\0" * 4096)
        left = [planted]
    # Wave 13 (decision 87): a temp whose in-flight marker the kill left is swept seconds later, whatever its age;
    # only a marker-less one (the planted temp) waits out Wave 12's one-hour rule, so only that one is aged.
    marked = [f.name.removesuffix(".lock") for f in left if f.name.endswith(".lock")]
    old = time.time() - 2 * 3600  # older than the sweep's in-flight window
    for f in left:
        if not any(f.name.startswith(m) for m in marked):
            os.utime(f, (old, old))
    if marked:
        _lib.note(f"{len(marked)} in-flight marker(s) left by the kill: the retry runs at once, nothing aged")
    claimed = out / name
    if claimed.exists() and not (out / f"{rid}.gate.json").exists():
        # A kill after the export claimed its name leaves the image without its sidecar: never adopted as a repeat
        # (409), so the artist removes it, as the export's own message says. Recorded, not a finding of this check.
        _lib.note(
            f"the kill left {name} ({claimed.stat().st_size} bytes) without its sidecar: removed by hand"
        )
        claimed.unlink()
    r = d.api("POST", f"/runs/{rid}/export", {"out_dir": str(out), "format": "exr"})
    check(r.status_code == 200, f"the retry -> {r.status_code} {r.text[:200]}")
    remaining = sorted(x.name for x in out.iterdir())
    check(
        not any(n.startswith(".") for n in remaining), f"after the retry the client folder holds {remaining}"
    )
    check(
        name in remaining and f"{rid}.gate.json" in remaining, f"the retry did not ship the pair: {remaining}"
    )


def _quiet(post: Any, path: str, body: dict[str, Any]) -> None:
    """A request whose answer nobody waits for (the daemon is killed under it)."""
    try:
        post(path, json=body)
    except Exception:
        pass


CHECKS = {
    "seedance-negative-prompt": seedance_negative_prompt,
    "fetching-repoll": fetching_repoll,
    "kling-seed-runway": kling_seed_runway,
    "corrupt-project-json": corrupt_project_json,
    "index-loss-rebuild": index_loss_rebuild,
    "ro-project-volume": ro_project_volume,
    "low-disk": low_disk,
    "sigkill-export-part": sigkill_export_part,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
