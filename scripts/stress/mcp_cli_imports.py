#!/usr/bin/env python3
"""The MCP / CLI / imports attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12,
T137/T143/T146).

    python scripts/stress/mcp_cli_imports.py [--keep] [--only <check>] [--list]

Like the attacker (scratchpad stress/mcp-cli-imports/repro.py f1-f12), this script drives the MCP tools in
process (`plate_daemon.mcp_server.PlateTools` over a loopback client of the running daemon) and runs the CLIs
(`python -m plate_daemon.cli`, `python -m plate_daemon.mcp_server --new-envelope`) as subprocesses on the
daemon's home. The images are built with the standard library; the EXIF JPEG is a cutdown the daemon writes,
with an APP1 Orientation tag put in front.

- export-review-guard: MCP export_review of a GATE_FAIL take is refused `not_reviewable` and writes no bundle,
  as POST /review does; after a human override it builds.
- nda-scrub: on an NDA project get_graph hands back no free text (`ACME` sentinels in a prompt, a negative
  prompt, an outDir, an override's reason and by, labels) and names what it withheld.
- fifo-imports: 45 imports of FIFOs are 422 not_a_file at once and /health keeps answering (POSIX only).
- concurrent-run-node and concurrent-add-node: 6 run_node calls on one node are 6 attempts that all run; 12
  add_node calls leave 12 nodes.
- outbound-log-failure: when outbound.jsonl cannot be written, run_node and add_node are `outbound_log_failed`
  before the daemon is asked to act (Wave 13, decision 91): no run, no spend, no node, and no id to look up.
- still-named-mp4: a PNG renamed .mp4 is refused unreadable_video, not imported as a one-frame clip.
- exif-orientation: a JPEG whose EXIF turns it 90 degrees becomes an upright Tier C packet.
- declared-cost-bound: an import's declared cost over 1,000 USD, or not finite, is 422 and makes no run.
- kit-camera-containment: a kit camera name that is not a plain name ('../escape') is 422 bad_camera, even with
  a camera JSON waiting outside the kit folder.
- cli-envelope-edges: non-finite or absurd envelope and CLI numbers are a one-line usage error, never a
  traceback; `--to` naming a file is refused with the path.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, error_of, png, skip

_lib.use_source_tree()

from plate_daemon import mcp_server as mcp  # noqa: E402

# The attacker's Windows-separator variant of "../escape": a hostile camera name, not a path of ours.
WINDOWS_ESCAPE = "\\".join(("..", "escape"))
_SHARED: Daemon | None = None
_SHIFT: Daemon | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="mcp-shared").start()
    return _SHARED


def shift() -> Daemon:
    """The echo fake moving every result 8 px: every Edit is GATE_FAIL."""
    global _SHIFT
    if _SHIFT is None or not _SHIFT.alive():
        _SHIFT = Daemon(name="mcp-shift", fake={"echo": True, "shift_px": 8}).start()
    return _SHIFT


@contextlib.contextmanager
def tools_for(d: Daemon) -> Iterator[Any]:
    """PlateTools over a loopback client of `d`; the token comes from its home, as the agent's server reads it."""
    saved = os.environ.get("PLATE_HOME")
    os.environ["PLATE_HOME"] = str(d.home)
    client = mcp.default_client(url=d.base)
    try:
        yield mcp.PlateTools(mcp.PlateBackend(client))
    finally:
        client.close()
        if saved is None:
            os.environ.pop("PLATE_HOME", None)
        else:
            os.environ["PLATE_HOME"] = saved


def envelope(d: Daemon, project: str, usd: float = 5.0) -> str:
    body = {
        "project_id": project,
        "owner": "agent:stress",
        "max_total_usd": usd,
        "max_per_run_usd": usd,
        "ttl_s": 3600,
    }
    r = d.api("POST", "/envelopes", body, ui=True)
    if r.status_code != 201:
        raise RuntimeError(f"envelope -> {r.status_code} {r.text[:200]}")
    return str(r.json()["id"])


def runs_of(d: Daemon, project: str) -> list[dict[str, Any]]:
    return d.api("GET", f"/projects/{project}/runs").json()


def flat_plate(path: Path, rgb: tuple[int, int, int] = (50, 60, 70)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png(640, 360, rgb))
    return path


def import_(d: Daemon, project: str, path: Path | str, packet: str, **extra: Any) -> Any:
    return d.api(
        "POST",
        f"/projects/{project}/import",
        {"path": str(path), "packet_id": packet, "source": "manual", **extra},
    )


def cli(d: Daemon, *argv: str, module: str = "plate_daemon.cli") -> tuple[int, str]:
    env = _lib.child_env({"PLATE_HOME": str(d.home), "PLATE_PORT": str(d.port)})
    done = subprocess.run(
        [sys.executable, "-m", module, *argv], cwd=_lib.ROOT, env=env, capture_output=True, timeout=180
    )
    return done.returncode, (done.stdout + done.stderr).decode("utf-8", errors="replace")


def exif_orientation(jpeg: bytes, orientation: int) -> bytes:
    """`jpeg` with an APP1 Exif segment (IFD0: Orientation) right after SOI."""
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
    tiff += struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
    body = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + jpeg[2:]


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's SOF segment."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + length
    raise ValueError("no SOF segment")


# ---------------------------------------------------------------------------- checks


def export_review_guard() -> None:
    d = shared()
    p = "r1"
    pkt, _ = d.edit_project(p)
    r = import_(d, p, flat_plate(d.home / "work" / "flat.png"), pkt)
    if not check(r.status_code == 201, f"import -> {r.status_code} {r.text[:200]}"):
        return
    run = r.json()
    check(run["state"] == "GATE_FAIL", f"the flat take is {run['state']}")
    http = d.api("POST", f"/projects/{p}/review", {"run_ids": [run["id"]]})
    check(
        http.status_code == 409 and error_of(http) == "not_reviewable", f"POST /review -> {http.status_code}"
    )
    review = d.home / "projects" / p / "review"
    with tools_for(d) as tools:
        try:
            out: Any = tools.export_review(p, [run["id"]])
        except mcp.McpError as e:
            out = e
        check(
            isinstance(out, mcp.McpError) and out.code == "not_reviewable",
            f"MCP export_review -> {out!r:.200}",
        )
        zips = sorted(review.glob("*.zip")) if review.exists() else []
        check(not zips, f"a bundle was written for a GATE_FAIL take: {[z.name for z in zips]}")
        o = d.api(
            "POST",
            f"/runs/{run['id']}/override",
            {"reason": "the client asked to see this take", "by": "human"},
            ui=True,
        )
        check(o.status_code == 200, f"override -> {o.status_code}")
        built = tools.export_review(p, [run["id"]])
        check(Path(built["data"]["bundle_path"]).is_file(), f"no bundle after the override: {built}")


def nda_scrub() -> None:
    d = shared()
    p = "r2"
    pkt, mask = d.edit_project(p, nda=True)
    g = _lib.edit_graph(pkt, mask)
    edit = g["nodes"][2]
    edit["label"] = "ACME lobby"
    edit["params"].update(
        {
            "prompt": "ACME-prompt",
            "negative_prompt": "no ACME-TOWER logo",
            "outDir": "/Volumes/ACME-CLIENT/deliver",
        }
    )
    edit["overrides"] = [
        {
            "reason": "ACME CEO insisted on the east facade",
            "by": "Priya at ACME",
            "at": "2026-10-18T10:00:00Z",
        }
    ]
    r = d.api("PUT", f"/projects/{p}/graph", g, ui=True)
    if not check(r.status_code == 200, f"PUT the NDA graph -> {r.status_code} {r.text[:200]}"):
        return
    with tools_for(d) as tools:
        out = tools.get_graph(p)
    blob = json.dumps(out, ensure_ascii=False)
    leaks = [
        s
        for s in ("ACME-TOWER", "ACME-CLIENT", "ACME CEO", "Priya at ACME", "ACME lobby", "ACME-prompt")
        if s in blob
    ]
    check(not leaks, f"NDA get_graph hands over {leaks}")
    check(bool(out.get("withheld")), "get_graph names nothing it withheld")
    nodes = (out.get("data") or {}).get("graph", {}).get("nodes", [])
    check(
        [n.get("id") for n in nodes] == ["plate", "mask", "edit", "export"],
        f"the structure is gone: {nodes[:2]}",
    )


def fifo_imports() -> None:
    if not hasattr(os, "mkfifo"):
        skip("no FIFOs on this OS")
    d = shared()
    p = "r7"
    pkt, _ = d.edit_project(p)
    fifos = d.home / "fifos"
    fifos.mkdir()
    pipes = []
    for i in range(45):
        os.mkfifo(fifos / f"p{i}.png")
        pipes.append(fifos / f"p{i}.png")
    clients = [d.make_client(timeout=15) for _ in pipes]
    t0 = time.monotonic()
    try:
        answers = _lib.concurrently(
            lambda i: clients[i].post(
                f"/projects/{p}/import", json={"path": str(pipes[i]), "packet_id": pkt, "source": "manual"}
            ),
            len(pipes),
            timeout_s=60,
        )
        ms = d.health_ms(10)
    finally:
        for c in clients:
            c.close()
        for pipe in pipes:  # release any reader still blocked on a pipe
            threading.Thread(target=lambda q=pipe: _quiet_open(q), daemon=True).start()
    dt = time.monotonic() - t0
    codes = {(_lib.status_of(r), error_of(r) if hasattr(r, "status_code") else None) for r in answers}
    check(codes == {(422, "not_a_file")}, f"45 FIFO imports: {codes}")
    check(dt < 10, f"45 FIFO imports took {dt:.1f} s")
    check(ms < 1000, f"/health took {ms:.0f} ms after the FIFO imports")
    check(not runs_of(d, p), "a FIFO import left a run")


def _quiet_open(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)


def concurrent_run_node() -> None:
    d = shared()
    p = "r3"
    d.edit_project(p)
    env = envelope(d, p, 5.0)
    out: list[Any] = [None] * 6

    def one(i: int) -> None:
        with tools_for(d) as tools:
            try:
                out[i] = tools.run_node(p, "edit", env, 1)["data"]["run_id"]
            except mcp.McpError as e:
                out[i] = e

    _lib.concurrently(one, 6)
    ids = [x for x in out if isinstance(x, str)]
    check(len(set(ids)) == 6, f"6 run_node calls gave {out}")
    for rid in ids:
        d.wait_run(rid, 120)
    runs = [r for r in runs_of(d, p) if r["id"] in ids]
    check(
        sorted(r["attempt"] for r in runs) == list(range(1, 7)),
        f"attempts {sorted(r['attempt'] for r in runs)}",
    )
    states = {r["state"] for r in d.api("GET", f"/projects/{p}/runs").json() if r["id"] in ids}
    check(states == {"DONE"}, f"the 6 runs ended {states}")


def concurrent_add_node() -> None:
    d = shared()
    p = "r4"
    d.edit_project(p)
    out: list[Any] = [None] * 12

    def one(i: int) -> None:
        with tools_for(d) as tools:
            try:
                out[i] = tools.add_node(p, "Colour", {}, "edit")["data"]["node_id"]
            except mcp.McpError as e:
                out[i] = e

    _lib.concurrently(one, 12)
    ids = [x for x in out if isinstance(x, str)]
    check(len(set(ids)) == 12, f"12 add_node calls gave {out}")
    g = d.api("GET", f"/projects/{p}/graph").json()
    stored = [n["id"] for n in g["nodes"] if n["kind"] == "Colour"]
    check(sorted(stored) == sorted(ids), f"persisted {sorted(stored)} of {sorted(ids)}")


def outbound_log_failure() -> None:
    d = shared()
    p = "r5"
    d.edit_project(p)
    env = envelope(d, p, 1.0)
    log = d.home / "projects" / p / "outbound.jsonl"
    with tools_for(d) as tools:
        tools.get_graph(p)  # the first call makes the log
        if not log.exists():
            skip("the MCP tools wrote no outbound.jsonl to make read-only")
        nodes = len(d.api("GET", f"/projects/{p}/graph").json()["nodes"])
        mode = log.stat().st_mode
        os.chmod(log, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            if os.access(log, os.W_OK):
                skip("this account writes read-only files (root?)")
            try:
                tools.run_node(p, "edit", env, 1)
                run_err: Any = None
            except mcp.McpError as e:
                run_err = e
            try:
                tools.add_node(p, "Colour", {}, "edit")
                node_err: Any = None
            except mcp.McpError as e:
                node_err = e
        finally:
            os.chmod(log, mode | stat.S_IWUSR)
    # Wave 13 (decision 91): the mutating tools probe the log before the daemon acts, so nothing is started.
    runs = runs_of(d, p)
    if check(
        isinstance(run_err, mcp.McpError) and run_err.code == "outbound_log_failed",
        f"run_node -> {run_err!r:.160}",
    ):
        check(not runs, f"run_node started {len(runs)} run(s) with the log unwritable")
        check("run_id" not in run_err.message, f"the error names a run: {run_err.message[:200]}")
    if check(
        isinstance(node_err, mcp.McpError) and node_err.code == "outbound_log_failed",
        f"add_node -> {node_err!r:.160}",
    ):
        g = d.api("GET", f"/projects/{p}/graph").json()
        check(len(g["nodes"]) == nodes, f"add_node added a node with the log unwritable: {len(g['nodes'])}")
        check("node_id" not in node_err.message, f"the error names a node: {node_err.message[:200]}")


def still_named_mp4() -> None:
    d = shared()
    p = "r8"
    pkt, _ = d.edit_project(p)
    still = d.home / "work" / "still_named.mp4"
    still.parent.mkdir(parents=True, exist_ok=True)
    still.write_bytes(png(640, 360, (50, 60, 70)))
    r = import_(d, p, still, pkt)
    check(
        r.status_code == 422 and error_of(r) == "unreadable_video",
        f"a PNG named .mp4 -> {r.status_code} {r.text[:200]}",
    )
    check(not runs_of(d, p), "the refused import left a run")


def exif_orientation_check() -> None:
    d = shared()
    p = "r9"
    d.edit_project(p)
    rid = d.done_run(p)
    out = d.home / "work" / "cut"
    r = d.api(
        "POST",
        f"/runs/{rid}/cutdown",
        {"preset": "16:9", "out_dir": str(out), "name": "wide", "format": "jpeg"},
    )
    if not check(r.status_code == 200, f"jpeg cutdown -> {r.status_code} {r.text[:200]}"):
        return
    jpg = next(out.glob("wide.jp*g"))
    w, h = jpeg_size(jpg.read_bytes())
    rotated = d.home / "work" / "rot6.jpg"
    rotated.write_bytes(exif_orientation(jpg.read_bytes(), 6))
    t = d.api(
        "POST",
        f"/projects/{p}/tierc",
        {"plate_path": str(rotated), "shot_id": "rot6", "consent_record": "client consent 2026-10-18"},
    )
    if not check(t.status_code == 202, f"tierc -> {t.status_code} {t.text[:200]}"):
        return
    manifest = json.loads((Path(t.json()["packet_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    res = manifest["camera"]["resolution"]
    check(
        (res["width"], res["height"]) == (h, w),
        f"a {w}x{h} JPEG turned by Orientation 6 became a {res['width']}x{res['height']} packet, not {h}x{w}",
    )


def declared_cost_bound() -> None:
    d = shared()
    p = "r13"
    pkt, _ = d.edit_project(p)
    plate = _lib.FIXTURES / "room_a" / "display.tif"
    for cost in (1e308, 1000.01, 1e6):
        r = import_(d, p, plate, pkt, cost_usd=cost)
        check(
            r.status_code == 422 and error_of(r) == "bad_cost",
            f"cost_usd {cost} -> {r.status_code} {r.text[:160]}",
        )
    body = (
        json.dumps({"path": str(plate), "packet_id": pkt, "source": "manual"})[:-1]
        + ', "cost_usd": Infinity}'
    )
    r = d.client.post(
        f"/projects/{p}/import", content=body.encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    check(r.status_code == 422, f"cost_usd Infinity -> {r.status_code}")
    check(not runs_of(d, p), "a refused declared cost left a run")
    r = import_(d, p, plate, pkt, cost_usd=1000.0)
    check(r.status_code == 201, f"cost_usd at the bound -> {r.status_code} {r.text[:160]}")


def kit_camera_containment() -> None:
    d = shared()
    p = "r10"
    d.project(p)
    kit = d.home / "work" / "kitdir" / "inner"
    kit.mkdir(parents=True)
    fixture = _lib.source_root() / "plugins" / "max" / "tests" / "kit_fixture"
    shutil.copy(fixture / "CAM_a_legend.txt", kit / "CAM_a_legend.txt")
    shutil.copy(_lib.FIXTURES / "room_a" / "beauty.exr", kit / "CAM_a_packet.exr")
    shutil.copy(fixture / "CAM_a_camera.json", kit.parent / "escape_camera.json")  # outside the kit folder
    before = sorted((d.home / "packets").rglob("manifest.json"))
    for camera in ("../escape", WINDOWS_ESCAPE, "a/b"):
        r = d.api(
            "POST",
            f"/projects/{p}/kit",
            {
                "folder": str(kit),
                "camera": camera,
                "plate_path": str(_lib.FIXTURES / "room_a" / "display.tif"),
                "shot_id": "esc",
            },
        )
        check(
            r.status_code == 422 and error_of(r) == "bad_camera",
            f"kit camera {camera!r} -> {r.status_code} {r.text[:160]}",
        )
    after = sorted((d.home / "packets").rglob("manifest.json"))
    check(after == before, "a refused kit camera built a packet")


def cli_envelope_edges() -> None:
    d = shared()
    p = "r12"
    d.edit_project(p)
    rid = d.done_run(p)
    cases = [
        (("--new-envelope", "--project", p, "--usd", "1", "--hours", "inf"), "plate_daemon.mcp_server", 2),
        (("--new-envelope", "--project", p, "--usd", "1", "--hours", "1e12"), "plate_daemon.mcp_server", 2),
        (("--new-envelope", "--project", p, "--usd", "inf", "--hours", "1"), "plate_daemon.mcp_server", 2),
        (
            (
                "pack",
                str(d.home / "projects" / p),
                "--out",
                str(d.home / "work" / "nan.zip"),
                "--max-blob-mb=nan",
            ),
            "plate_daemon.cli",
            4,
        ),
        (("gc", str(d.home / "projects" / p), "--min-age-hours=inf"), "plate_daemon.cli", 4),
    ]
    for argv, module, want in cases:
        code, text = cli(d, *argv, module=module)
        check(
            code == want and "Traceback" not in text,
            f"{module} {' '.join(argv[-2:])}: rc {code} {text.strip()[-160:]}",
        )
    afile = d.home / "work" / "afile"
    afile.parent.mkdir(parents=True, exist_ok=True)
    afile.write_text("x", encoding="utf-8", newline="\n")
    code, text = cli(d, "export", rid, "--project", str(d.home / "projects" / p), "--to", str(afile))
    check(
        code == 4 and "--to must be a folder, got a file" in text,
        f"export --to <a file>: rc {code} {text.strip()[-160:]}",
    )
    check(afile.read_text(encoding="utf-8") == "x", "the file named by --to was written")
    r = d.client.post(
        "/envelopes",
        content=f'{{"project_id":"{p}","owner":"agent:x","max_total_usd":Infinity,"max_per_run_usd":1,"ttl_s":3600}}'.encode(),
        headers={"Content-Type": "application/json"},
    )
    check(r.status_code == 422, f"POST /envelopes Infinity -> {r.status_code}")


CHECKS = {
    "export-review-guard": export_review_guard,
    "nda-scrub": nda_scrub,
    "fifo-imports": fifo_imports,
    "concurrent-run-node": concurrent_run_node,
    "concurrent-add-node": concurrent_add_node,
    "outbound-log-failure": outbound_log_failure,
    "still-named-mp4": still_named_mp4,
    "exif-orientation": exif_orientation_check,
    "declared-cost-bound": declared_cost_bound,
    "kit-camera-containment": kit_camera_containment,
    "cli-envelope-edges": cli_envelope_edges,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
