#!/usr/bin/env python3
"""The doctrine attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T129/T145/T143).

    python scripts/stress/doctrine.py [--keep] [--only <check>] [--list]

The attacker's repro_base.py, repro_override.py and repro_mask_late.py, turned around:

- base-any-cas-blob: (A) an uploaded reference relayed through a Colour node's outputHash is not a base
  (422 base_not_a_take); (B) a GATE_FAIL import picked as a base is refused until it is overridden, and the child
  then fails its own gate instead of laundering the drift (no export without its own override); (C) a depth-2 take
  relayed through a Colour node is 422 DEPTH_EXCEEDED with the chain named.
- override-not-ledgered: with runs.jsonl read-only (D) a typed-override export is 503 ledger_unavailable and
  writes no file, (E) POST /override is 503 and a batch export still refuses the take; (F) on a writable ledger the
  override record is written before the exported file.
- cli-sidecar-reason: `plate export --override-reason B` after an override A ledgers B before the file and the
  sidecar carries B.
- mask-errors-at-quote: unknown ids, a non-string id and a mask of the wrong size are 422 at the quote with no run
  row, not FAILED after a human approved them.

The images are flat PNGs built with the standard library (the attacker blurred the plate with cv2; any plate-sized
picture that is not the plate exercises the same path). The CLI runs as a subprocess on the same home.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, error_of, png, skip

PROJECT = "repro"
OVERRIDE_A = "reason A: AD approved the drift"
OVERRIDE_B = "reason B: typed at the CLI instead"
_PLAIN: Daemon | None = None
_SHIFT: Daemon | None = None


def plain() -> Daemon:
    """The attacker's clean daemon (the seed fake engine) with project `repro` on room_a."""
    global _PLAIN
    if _PLAIN is None or not _PLAIN.alive():
        _PLAIN = Daemon(name="doctrine-plain").start()
        _PLAIN.edit_project(PROJECT)
    return _PLAIN


def shift() -> Daemon:
    """The attacker's shift daemon: the echo fake moves every result 8 px, so every Edit is GATE_FAIL."""
    global _SHIFT
    if _SHIFT is None or not _SHIFT.alive():
        _SHIFT = Daemon(name="doctrine-shift", fake={"echo": True, "shift_px": 8}).start()
        _SHIFT.edit_project(PROJECT)
    return _SHIFT


def node(nid: str, kind: str, **kw: Any) -> dict[str, Any]:
    return {"id": nid, "kind": kind, "label": nid, **kw}


def edit(nid: str, **kw: Any) -> dict[str, Any]:
    return node(nid, "Edit", params={"engine": "fake", "prompt": nid}, **kw)


def edge(s: str, t: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{s}->{t}", "source": s, "target": t, **({"targetHandle": handle} if handle else {})}


def packet_and_mask(d: Daemon) -> tuple[str, str]:
    pid = d.api("GET", f"/projects/{PROJECT}/packets").json()[0]["packet_id"]
    return pid, d.mask_id(pid)


def put(d: Daemon, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> Any:
    r = d.api("PUT", f"/projects/{PROJECT}/graph", {"nodes": nodes, "edges": edges}, ui=True)
    if r.status_code != 200:
        raise RuntimeError(f"PUT graph -> {r.status_code} {r.text[:300]}")
    return r


def runs_of(d: Daemon, node_id: str) -> list[dict[str, Any]]:
    return [r for r in d.api("GET", f"/projects/{PROJECT}/runs").json() if r.get("node_id") == node_id]


def make(d: Daemon, node_id: str) -> dict[str, Any]:
    rid = d.quote_run(PROJECT, node_id)
    a = d.approve(rid)
    if a.status_code != 200:
        raise RuntimeError(f"approve {rid} -> {a.status_code} {a.text[:300]}")
    return d.wait_run(rid)


def ledger_path(d: Daemon) -> Path:
    return d.home / "projects" / PROJECT / "runs.jsonl"


@contextlib.contextmanager
def read_only(path: Path) -> Any:
    """runs.jsonl read-only for the body (POSIX mode bits, the Windows read-only attribute)."""
    mode = path.stat().st_mode
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    try:
        if os.access(path, os.W_OK):
            skip("this account writes read-only files (root?)")
        yield
    finally:
        os.chmod(path, mode | stat.S_IWUSR)


def records(d: Daemon, kind: str, run_id: str) -> list[dict[str, Any]]:
    return [x for x in d.ledger(PROJECT) if x.get("kind") == kind and x.get("run_id") == run_id]


def gate_fail(d: Daemon, tag: str) -> str:
    rid = d.quote_run(PROJECT, prompt=tag)
    d.approve(rid)
    r = d.wait_run(rid)
    if r.get("state") != "GATE_FAIL":
        raise RuntimeError(f"the shift daemon's take {rid} ended {r.get('state')}, want GATE_FAIL")
    return rid


# ---------------------------------------------------------------------------- checks


def base_any_cas_blob() -> None:
    d = plain()
    pid, mid = packet_and_mask(d)
    plate = node("plate", "Plate", packetId=pid, tier="A")
    mask = node("mask", "Mask", params={"ids": [mid]})

    # (A) a reference upload relayed as a base
    r = d.client.post(
        "/assets",
        params={"kind": "reference", "project_id": PROJECT},
        content=png(640, 360, (200, 40, 40)),
        headers={"Content-Type": "image/png"},
    )
    if not check(r.status_code == 201, f"reference upload -> {r.status_code} {r.text[:200]}"):
        return
    ref = r.json()["hash"]
    put(
        d,
        [plate, mask, node("col", "Colour", outputHash=ref), edit("a")],
        [edge("plate", "mask"), edge("plate", "col"), edge("col", "a", "image"), edge("mask", "a", "mask")],
    )
    q = d.quote(PROJECT, "a")
    body = q.json() if _lib.is_json(q) else {}
    check(
        q.status_code == 422 and body.get("error") == "base_not_a_take" and body.get("node") == "col",
        f"(A) a reference upload as the base -> {q.status_code} {q.text[:200]}",
    )
    check(not runs_of(d, "a"), "(A) the refused quote left a run row")

    # (B) a GATE_FAIL import picked as a base
    flat = d.home / "imports" / "flat.png"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(png(640, 360, (90, 90, 90)))
    nodes = [plate, mask, edit("picked"), edit("b")]
    edges = [
        edge("plate", "mask"),
        edge("mask", "picked", "mask"),
        edge("picked", "b", "image"),
        edge("mask", "b", "mask"),
    ]
    put(d, nodes, edges)
    r = d.api(
        "POST",
        f"/projects/{PROJECT}/import",
        {"path": str(flat), "packet_id": pid, "node_id": "picked", "source": "manual"},
    )
    if not check(r.status_code == 201, f"(B) import -> {r.status_code} {r.text[:200]}"):
        return
    imp = r.json()
    check(
        imp["state"] == "GATE_FAIL" and imp["exportable"] is False, f"(B) the flat import is {imp['state']}"
    )
    nodes[2] = {**nodes[2], "outputHash": imp["outputs"][0]}
    put(d, nodes, edges)
    q = d.quote(PROJECT, "b")
    body = q.json() if _lib.is_json(q) else {}
    check(
        q.status_code == 422 and body.get("error") == "base_gate_fail" and body.get("run_id") == imp["id"],
        f"(B) a GATE_FAIL take as the base, no override -> {q.status_code} {q.text[:200]}",
    )
    check(not runs_of(d, "b"), "(B) the refused quote left a run row")
    o = d.api(
        "POST",
        f"/runs/{imp['id']}/override",
        {"reason": "the client asked for this soft look", "by": "human"},
    )
    check(o.status_code == 200, f"(B) override of the import -> {o.status_code} {o.text[:200]}")
    child = make(d, "b")
    check(
        child.get("state") == "GATE_FAIL" and child.get("inputs", {}).get("base_run") == imp["id"],
        f"(B) the child of an overridden GATE_FAIL base is {child.get('state')} (want GATE_FAIL, base_run named)",
    )
    out = d.home / "out_b"
    e = d.api(
        "POST", f"/runs/{child['id']}/export", {"out_dir": str(out), "format": "png16", "full_frame": True}
    )
    check(
        e.status_code == 409 and error_of(e) == "not_exportable",
        f"(B) export of the child, no override -> {e.status_code}",
    )
    check(not out.exists() or not any(out.iterdir()), "(B) the refused export left files")

    # (C) generation depth 3 through a relay
    nodes = [plate, mask, edit("d1")]
    edges = [edge("plate", "mask"), edge("plate", "d1"), edge("mask", "d1", "mask")]
    put(d, nodes, edges)
    r1 = make(d, "d1")
    nodes[2] = {**nodes[2], "outputHash": r1["outputs"][0]}
    nodes.append(edit("d2"))
    edges += [edge("d1", "d2", "image"), edge("mask", "d2", "mask")]
    put(d, nodes, edges)
    r2 = make(d, "d2")
    if not check(
        r1.get("state") == "DONE" and r2.get("state") == "DONE",
        f"(C) d1 {r1.get('state')}, d2 {r2.get('state')}",
    ):
        return
    relay = node("col", "Colour", outputHash=r2["outputs"][0])
    put(
        d,
        [*nodes, relay, edit("d3")],
        [*edges, edge("plate", "col"), edge("col", "d3", "image"), edge("mask", "d3", "mask")],
    )
    q = d.quote(PROJECT, "d3")
    body = q.json() if _lib.is_json(q) else {}
    check(
        q.status_code == 422
        and body.get("error") == "DEPTH_EXCEEDED"
        and body.get("chain") == [r2["id"], r1["id"]],
        f"(C) a depth-2 take relayed through a Colour node -> {q.status_code} {q.text[:200]}",
    )
    check(not runs_of(d, "d3"), "(C) the refused quote left a run row")


def override_not_ledgered() -> None:
    d = shift()
    led = ledger_path(d)
    out = d.home / "out_override"
    # (D) a typed override on export while the ledger cannot take it
    a = gate_fail(d, "d")
    with read_only(led):
        r = d.api(
            "POST",
            f"/runs/{a}/export",
            {
                "out_dir": str(out / "D"),
                "format": "png16",
                "override": True,
                "reason": "client accepted the drift in writing",
            },
        )
    check(
        r.status_code == 503 and error_of(r) == "ledger_unavailable",
        f"(D) typed-override export on a read-only ledger -> {r.status_code} {r.text[:200]}",
    )
    files = sorted(p.name for p in (out / "D").iterdir()) if (out / "D").exists() else []
    check(not files, f"(D) files shipped with no override on record: {files}")
    check(
        not records(d, "override", a) and not records(d, "export", a),
        "(D) records written for a refused export",
    )
    view = d.run(a)
    check(
        view.get("override") is None and view.get("exportable") is False,
        f"(D) the run is now {view.get('override')}",
    )

    # (E) POST /override while the ledger cannot take it, then a batch export
    b = gate_fail(d, "e")
    with read_only(led):
        r = d.api("POST", f"/runs/{b}/override", {"reason": "art director approved the drift", "by": "human"})
    check(
        r.status_code == 503 and error_of(r) == "ledger_unavailable",
        f"(E) POST /override on a read-only ledger -> {r.status_code} {r.text[:200]}",
    )
    r = d.api(
        "POST",
        f"/projects/{PROJECT}/export",
        {"run_ids": [b], "out_dir": str(out / "E"), "format": "png16", "name_pattern": "E_{run8}"},
    )
    exported = r.json().get("exported", []) if _lib.is_json(r) else []
    check(
        not exported, f"(E) the batch export shipped the un-overridden take: {r.status_code} {r.text[:200]}"
    )
    check(not records(d, "override", b), "(E) an override record appeared for the refused override")

    # (F) the typed override is on the record before the file exists
    f = gate_fail(d, "f")
    r = d.api(
        "POST",
        f"/runs/{f}/export",
        {
            "out_dir": str(out / "F"),
            "format": "png16",
            "override": True,
            "reason": "client signed off on the drift",
        },
    )
    if not check(r.status_code == 200, f"(F) typed-override export -> {r.status_code} {r.text[:200]}"):
        return
    path = Path(r.json()["path"])
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    ovr = records(d, "override", f)
    exp = records(d, "export", f)
    if check(len(ovr) == 1 and len(exp) == 1, f"(F) {len(ovr)} override and {len(exp)} export records"):
        at = datetime.fromisoformat(str(ovr[0]["at"]).replace("Z", "+00:00"))
        check(
            at <= mtime,
            f"(F) the override was ledgered at {at.isoformat()}, after the file ({mtime.isoformat()})",
        )
        check(ovr[0]["seq"] < exp[0]["seq"], "(F) the export record precedes the override record")


def cli_sidecar_reason() -> None:
    d = shift()
    g = gate_fail(d, "g")
    r = d.api("POST", f"/runs/{g}/override", {"reason": OVERRIDE_A, "by": "human"})
    if not check(r.status_code == 200, f"override A -> {r.status_code} {r.text[:200]}"):
        return
    out = d.home / "out_cli"
    env = _lib.child_env({"PLATE_HOME": str(d.home), "PLATE_PORT": str(d.port)})
    argv = [sys.executable, "-m", "plate_daemon.cli", "export", g, "--to", str(out), "--format", "png16"]
    argv += ["--name", "G_cli", "--override-reason", OVERRIDE_B]
    done = subprocess.run(argv, cwd=_lib.ROOT, env=env, capture_output=True, timeout=300)
    text = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    if not check(done.returncode == 0, f"plate export exited {done.returncode}: {text[-400:]}"):
        return
    reasons = [x.get("reason") for x in records(d, "override", g)]
    check(reasons == [OVERRIDE_A, OVERRIDE_B], f"ledgered override reasons {reasons}")
    exp = records(d, "export", g)
    ovr = records(d, "override", g)
    if exp and len(ovr) == 2:
        check(ovr[1]["seq"] < exp[-1]["seq"], "reason B was ledgered after the export record")
    sidecar = out / "G_cli.gate.json"
    side = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    got = (side.get("export") or {}).get("override_reason")
    check(got == OVERRIDE_B, f"sidecar override_reason {got!r}")


def mask_errors_at_quote() -> None:
    d = plain()
    pid, _ = packet_and_mask(d)
    r = d.client.post(
        "/assets",
        params={"kind": "mask", "project_id": PROJECT},
        content=png(160, 90, (255,), grey=True),
        headers={"Content-Type": "image/png"},
    )
    if not check(r.status_code == 201, f"small mask upload -> {r.status_code} {r.text[:200]}"):
        return
    small = r.json()["hash"]
    cases = [
        ("ids not in the legend", {"params": {"ids": ["o:deadbeef"]}}, "bad_mask"),
        ("an id that is not a string", {"params": {"ids": [1]}}, "bad_mask"),
        ("outputHash of a 160x90 mask", {"params": {}, "outputHash": small}, "mask_size"),
    ]
    for label, mnode, code in cases:
        put(
            d,
            [node("plate", "Plate", packetId=pid, tier="A"), node("mask", "Mask", **mnode), edit("mx")],
            [edge("plate", "mask"), edge("mask", "mx", "mask")],
        )
        q = d.quote(PROJECT, "mx")
        check(
            q.status_code == 422 and error_of(q) == code, f"{label}: quote -> {q.status_code} {q.text[:200]}"
        )
    check(not runs_of(d, "mx"), f"refused quotes left {len(runs_of(d, 'mx'))} run rows")


CHECKS = {
    "base-any-cas-blob": base_any_cas_blob,
    "override-not-ledgered": override_not_ledgered,
    "cli-sidecar-reason": cli_sidecar_reason,
    "mask-errors-at-quote": mask_errors_at_quote,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
