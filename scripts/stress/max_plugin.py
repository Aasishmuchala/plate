#!/usr/bin/env python3
"""The Max-plugin attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T140-T142).

    python scripts/stress/max_plugin.py [--keep] [--only <check>] [--list]

Like the attacker (scratchpad stress/max-plugin/work), this script imports `plate_max` and its test stubs from
plugins/max/tests: the stateful fake pymxs scene, the simulated V-Ray and the stdlib stub daemon of
test_send_packet, and the scout result builder of test_jobs_poller. The job checks run the real daemon.

- dup-node-names: two geometry nodes named 'Shared' on ARCH_walls and VEG_tree_01 stop the Send at lint
  (DUPLICATE_NODE_NAME) in either scene order, with nothing rendered.
- dup-cameras: two cameras named CAM_dup stop at lint (DUPLICATE_CAMERA_NAME); neither is sent as the other.
- nonlatin-names: a camera name with no ASCII letter or digit is BAD_SHOT_SLUG at lint (no render, no folder);
  a scene file name in a non-Latin script leaves the project unresolved instead of 'untitled' or 'a'.
- nonfinite-pose: a NaN or infinite camera pose is BAD_POSE at lint, before the render.
- degenerate-lens: focal -1 / NaN, f-number 0, film -36 / 0, zoom 0, ISO 0, shutter 0 are BAD_LENS naming the
  property, before the render.
- corrupt-sidecar: a <scene>.plate.json that exists but cannot be used blocks the senders with its message and
  is left byte-identical; it never re-binds the scene to the stem's project.
- job-guard: against the real daemon's queue, a job whose packet the dock cannot find (B) or that was sent from
  an unsaved scene (C) runs with a "scene check skipped" warning; one for another project than the scene is
  bound to (D, the same master.max name in another client's folder) is refused.
- claimed-job-recovery: a job claimed for a dock whose answer was lost is taken up again when the dock starts,
  run once and closed on the real daemon.
- trickle-handshake: a peer that answers one byte a second cannot hold handshake(3 s) past about 3 s.
- token-second-line: a token file with a second '# rotated' line refuses with Unauthorized, and the secret is in
  no message.
- hash-503-retry: a /hash that answers 503 once while the daemon restarts costs one retry, not the render.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socketserver
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, note

_lib.use_source_tree()
sys.path.insert(0, str(_lib.source_root() / "plugins" / "max" / "tests"))

from fake_pymxs import SCENE_CAMERA_ROWS, FakeCamera, load_scene, make_node  # noqa: E402
from test_jobs_poller import _scout_results  # noqa: E402
from test_send_packet import Daemon as StubDaemon  # noqa: E402
from test_send_packet import send, serve_daemon, vray_scene  # noqa: E402

from plate_max import PLUGIN_VERSION, config, jobs, policy, recipes, rt, transport  # noqa: E402
from plate_max.actions import scout, send_packet  # noqa: E402
from plate_max.ui import controller as ctl  # noqa: E402

NAN, INF = float("nan"), float("inf")
SECRET = "fedcba9876543210fedcba9876543210"
ROTATED = SECRET + "\n# rotated 2026-10-01"
_REAL: Daemon | None = None


# ---------------------------------------------------------------------------- isolation and helpers


@contextlib.contextmanager
def plugin_env(tag: str) -> Iterator[Path]:
    """A fresh PLATE_HOME / PLATE_CONFIG for the plugin code in this process; restored afterwards."""
    base = _lib.RUN.scratch(f"max-{tag}")
    saved = {k: os.environ.get(k) for k in ("PLATE_HOME", "PLATE_CONFIG", "PLATE_TOKEN_FILE")}
    os.environ["PLATE_HOME"] = str(base / "plate-home")
    os.environ["PLATE_CONFIG"] = str(base / "plate-config" / "config.json")
    os.environ.pop("PLATE_TOKEN_FILE", None)
    getattr(config, "_reported", set()).clear()
    try:
        yield base
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rt.set_runtime(None)


def install(name: str) -> Any:
    scene = load_scene(name)
    rt.set_runtime(scene)
    return scene


@contextlib.contextmanager
def stub_daemon(base: Path) -> Iterator[tuple[Any, dict[str, Any], Path]]:
    packets = base / "packets"
    packets.mkdir(parents=True, exist_ok=True)
    daemon = StubDaemon(packets)
    with serve_daemon(daemon) as cfg:
        yield daemon, cfg, packets


def nothing_rendered(scene: Any, vray: Any, packets: Path, daemon: Any) -> bool:
    return (
        scene.render_calls == []
        and vray.written == []
        and list(packets.iterdir()) == []
        and daemon.hasher.requests == []
        and daemon.posted == []
    )


def codes(result: Any) -> list[str]:
    return [p["code"] for p in result.problems]


def add_camera(scene: Any, name: str, rows: Any = SCENE_CAMERA_ROWS, **props: Any) -> Any:
    lens = {"focal_length": 24.0, "f_number": 8.0, "shutter_speed": 125, **props}
    return scene.add_camera(FakeCamera(name, rows, **lens))


def rows_with(i: int, j: int, value: float) -> tuple[tuple[float, ...], ...]:
    rows = [list(r) for r in SCENE_CAMERA_ROWS]
    rows[i][j] = value
    return tuple(tuple(r) for r in rows)


def real_daemon() -> Daemon:
    global _REAL
    if _REAL is None or not _REAL.alive():
        _REAL = Daemon(name="max-real").start()
    return _REAL


class Timer:
    """What Poller.start needs of a QTimer: timeout.connect, start(ms), stop()."""

    def __init__(self) -> None:
        self.slots: list[Any] = []
        self.timeout = self

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def start(self, _ms: int) -> None:
        pass

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------- the Send preflight


def dup_node_names() -> None:
    with plugin_env("dup-nodes") as base:
        for order in ("AV", "VA"):
            scene, vray = vray_scene(install)
            nodes = {
                "A": make_node("Shared", "ARCH_walls", "MAT_brick", x=0.0),
                "V": make_node("Shared", "VEG_tree_01", "MAT_leaf", x=100.0),
            }
            scene.objects = [c for c in scene.objects if c.super_class != "GeometryClass"]
            scene.objects += [nodes[k] for k in order] + [make_node("ARCH_other", "ARCH_walls", "MAT_brick")]
            vray.shares = {"Shared": 0.5, "ARCH_other": 0.45}
            with stub_daemon(base / order) as (daemon, cfg, packets):
                [result] = send(["CAM_front"], packets, cfg)
                check(
                    not result.ok
                    and result.stage == "lint"
                    and codes(result) == [policy.DUPLICATE_NODE_NAME],
                    f"order {order}: ok={result.ok} stage={result.stage} codes={codes(result)} {result.errors[:1]}",
                )
                check(
                    nothing_rendered(scene, vray, packets, daemon), f"order {order}: rendered before refusing"
                )


def dup_cameras() -> None:
    with plugin_env("dup-cameras") as base:
        scene, vray = vray_scene(install)
        add_camera(scene, "CAM_dup")
        scene.cameras["CAM_dup (second)"] = FakeCamera("CAM_dup", SCENE_CAMERA_ROWS, focal_length=50.0)
        with stub_daemon(base) as (daemon, cfg, packets):
            results = send(["CAM_dup", "CAM_dup"], packets, cfg)
            for r in results:
                check(
                    not r.ok and r.stage == "lint" and codes(r) == [policy.DUPLICATE_CAMERA_NAME],
                    f"CAM_dup: ok={r.ok} stage={r.stage} codes={codes(r)}",
                )
            check(nothing_rendered(scene, vray, packets, daemon), "a duplicate camera name was rendered")


def nonlatin_names() -> None:
    with plugin_env("names") as base:
        for name in ("客户A", "كاميرا_رئيسية", "🎥🏠", "   ", "कैमरा_१", "カメラ主", ".."):
            scene, vray = vray_scene(install)
            add_camera(scene, name)
            with stub_daemon(base / f"cam{abs(hash(name)) % 10_000}") as (daemon, cfg, packets):
                [r] = send([name], packets, cfg)
                check(
                    not r.ok and r.stage == "lint" and codes(r) == [policy.BAD_SHOT_SLUG],
                    f"camera {name!r}: ok={r.ok} stage={r.stage} codes={codes(r)}",
                )
                check(nothing_rendered(scene, vray, packets, daemon), f"camera {name!r} was rendered")
        for stem in ("客户A_别墅.max", "客户B_酒店.max", "क्लबहाउस.max", "نادي_الفيلا.max"):
            scene = install("clubhouse")
            folder = base / "scenes"
            folder.mkdir(exist_ok=True)
            scene.maxFilePath, scene.maxFileName = str(folder), stem
            try:
                got: Any = ctl.resolve_project_id(folder / stem, {})
            except ctl.ProjectUnresolved as e:
                got = e
            check(isinstance(got, ctl.ProjectUnresolved), f"scene {stem!r} resolved to project {got!r}")


def nonfinite_pose() -> None:
    with plugin_env("pose") as base:
        for label, rows in (
            ("nan-position", rows_with(3, 0, NAN)),
            ("inf-position", rows_with(3, 1, INF)),
            ("inf-axis", rows_with(0, 0, INF)),
            ("nan-axis", rows_with(0, 0, NAN)),
        ):
            scene, vray = vray_scene(install)
            add_camera(scene, "CAM_pose", rows=rows)
            with stub_daemon(base / label) as (daemon, cfg, packets):
                [r] = send(["CAM_pose"], packets, cfg)
                check(
                    not r.ok and r.stage == "lint" and codes(r) == [policy.BAD_POSE],
                    f"{label}: ok={r.ok} stage={r.stage} codes={codes(r)}",
                )
                check(nothing_rendered(scene, vray, packets, daemon), f"{label}: rendered before refusing")


LENS = {
    "focal-minus-1": ({"focal_length": -1.0}, "focal_length"),
    "f-number-0": ({"f_number": 0.0}, "f_number"),
    "focal-nan": ({"focal_length": NAN}, "focal_length"),
    "film-minus-36": ({"film_width": -36.0}, "film_width"),
    "film-0": ({"film_width": 0.0}, "film_width"),
    "zoom-0": ({}, "zoom_factor"),
    "iso-0": ({"ISO": 0}, "iso"),
    "shutter-0": ({"shutter_speed": 0}, "shutter_speed"),
}


def degenerate_lens() -> None:
    with plugin_env("lens") as base:
        for label, (props, prop) in LENS.items():
            scene, vray = vray_scene(install)
            cam = add_camera(scene, "CAM_lens", **props)
            if label == "zoom-0":
                cam.zoom_factor = 0.0
            with stub_daemon(base / label) as (daemon, cfg, packets):
                [r] = send(["CAM_lens"], packets, cfg)
                detail = r.problems[0]["detail"] if r.problems else ""
                check(
                    not r.ok and r.stage == "lint" and codes(r) == [policy.BAD_LENS] and prop in detail,
                    f"{label}: ok={r.ok} stage={r.stage} codes={codes(r)} {detail[:120]!r}",
                )
                check(
                    "ZeroDivisionError" not in detail and "math domain" not in detail,
                    f"{label}: {detail[:120]}",
                )
                check(nothing_rendered(scene, vray, packets, daemon), f"{label}: rendered before refusing")


UNREADABLE = {
    "trailing comma": b'{"project_id": "nda-client-a",}',
    "utf-16 bom": '{"project_id": "nda-client-a"}'.encode("utf-16"),
    "empty": b"",
    "conflict markers": b'<<<<<<< HEAD\n{"project_id": "a"}\n=======\n{"project_id": "b"}\n>>>>>>> topic\n',
    "project_id 7": b'{"project_id": 7}',
}


def corrupt_sidecar() -> None:
    with plugin_env("sidecar") as base:
        for label, raw in UNREADABLE.items():
            scene = install("clubhouse")
            folder = base / label.replace(" ", "-")
            folder.mkdir()
            scene.maxFilePath, scene.maxFileName = str(folder), "villa-v3.max"
            side = recipes.sidecar_path(folder / "villa-v3.max")
            side.write_bytes(raw)
            live = ctl.LiveController()
            try:
                got: Any = live.project_id()
            except recipes.SidecarUnreadable as e:
                got = e
            check(isinstance(got, recipes.SidecarUnreadable), f"{label}: the scene bound to {got!r}")
            [refusal] = live.send_packet(["CAM_front"], lambda *a: None)
            check(
                refusal["ok"] is False and "unreadable" in str(refusal["errors"]), f"{label}: send {refusal}"
            )
            with contextlib.suppress(recipes.SidecarUnreadable, Exception):
                live.set_project("villa-v3")
            check(side.read_bytes() == raw, f"{label}: the sidecar was rewritten")


# ---------------------------------------------------------------------------- the job poller (real daemon)


def _job_packet(d: Daemon, project: str) -> str:
    if d.project(project).status_code not in (200, 201):
        raise RuntimeError(f"project {project}")
    return str(d.post_packet(d.stage_packet(project))["packet_id"])


def job_guard() -> None:
    d = real_daemon()
    with plugin_env("guard") as base:
        packets = base / "dock-packets"
        packets.mkdir()
        cfg = {"token": d.token, "daemon_url": d.base, "packets_dir": str(packets)}
        scene = install("clubhouse")
        client_a = base / "client-a"
        client_a.mkdir()
        scene.maxFilePath, scene.maxFileName = str(client_a), "master.max"  # bound by its stem: "master"
        ran: list[int] = []
        real_run = scout.run
        scout.run = lambda *a, **k: ran.append(1) or _scout_results(1)
        try:
            pid = _job_packet(d, "client-b")
            cases = {}
            for case in ("B", "C", "D"):
                folder = packets / pid
                if folder.exists():
                    for f in folder.iterdir():
                        f.unlink()
                    folder.rmdir()
                if case in ("C", "D"):
                    folder.mkdir()
                    max_file = None if case == "C" else {"name": "master.max"}
                    manifest = {"packet_id": pid, "scene_hash": {"max_file": max_file}}
                    (folder / "manifest.json").write_text(
                        json.dumps(manifest), encoding="utf-8", newline="\n"
                    )
                r = d.api(
                    "POST", "/plugin/jobs", {"action": "scout", "packet_id": pid, "count": 1, "keep": False}
                )
                if not check(r.status_code == 201, f"{case}: enqueue -> {r.status_code} {r.text[:160]}"):
                    return
                job = r.json()
                logged: list[str] = []
                before = len(ran)
                poller = jobs.Poller(cfg, log=lambda m, err=False, sink=logged: sink.append(m))
                handled = []
                for _ in range(5):
                    handled += poller.tick()
                    if any(h["id"] == job["id"] for h in handled):
                        break
                rec = next((h for h in handled if h["id"] == job["id"]), None)
                cases[case] = (rec and rec["body"].get("ok"), len(ran) > before, logged)
            ok_b, ran_b, log_b = cases["B"]
            check(
                ok_b is True and ran_b and any("scene check skipped" in m for m in log_b), f"B: {cases['B']}"
            )
            ok_c, ran_c, log_c = cases["C"]
            check(ok_c is True and ran_c and any("unsaved scene" in m for m in log_c), f"C: {cases['C']}")
            ok_d, ran_d, _ = cases["D"]
            check(ok_d is False and not ran_d, f"D (another project's master.max) ran: {cases['D'][:2]}")
        finally:
            scout.run = real_run


def claimed_job_recovery() -> None:
    d = real_daemon()
    with plugin_env("claim") as base:
        packets = base / "dock-packets"
        packets.mkdir()
        cfg = {"token": d.token, "daemon_url": d.base, "packets_dir": str(packets)}
        install("clubhouse")
        ran: list[int] = []
        real_run = scout.run
        scout.run = lambda *a, **k: ran.append(1) or _scout_results(1)
        try:
            pid = _job_packet(d, "stress-claim")
            poller = jobs.Poller(cfg)  # the dock that was running when its claim's answer was lost
            time.sleep(0.05)
            job = d.api(
                "POST", "/plugin/jobs", {"action": "scout", "packet_id": pid, "count": 1, "keep": False}
            ).json()
            lost = d.api("GET", "/plugin/jobs", params={"claim": 1, "since": 0, "limit": 1}).json()
            check([j["id"] for j in lost.get("jobs", [])] == [job["id"]], f"the lost claim took {lost}")
            check(
                d.api("GET", f"/plugin/jobs/{job['id']}").json().get("state") == "claimed",
                "the job is not claimed",
            )
            poller.start(Timer())  # the dock shown again (or its timer restarted after a lost answer)
            handled = []
            for _ in range(3):
                handled += poller.tick()
            state = d.api("GET", f"/plugin/jobs/{job['id']}").json().get("state")
            check(any(h["id"] == job["id"] for h in handled), f"the claimed job was not taken up: {handled}")
            check(state == "done", f"the recovered job is {state}")
            check(len(ran) == 1, f"the job ran {len(ran)} times")
        finally:
            scout.run = real_run


# ---------------------------------------------------------------------------- transport


@contextlib.contextmanager
def trickle_server(drip: bytes) -> Iterator[str]:
    """A peer that reads the request, then sends `drip` one byte a second, forever."""
    stop = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                self.request.recv(65536)
                n = 0
                while not stop.is_set():
                    self.request.sendall(drip[n % len(drip) : n % len(drip) + 1])
                    n += 1
                    stop.wait(1.0)
            except OSError:
                pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        t.join(5)


def trickle_handshake() -> None:
    with (
        plugin_env("trickle"),
        trickle_server(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n") as url,
    ):
        out: dict[str, Any] = {}

        def shake() -> None:
            try:
                transport.handshake(url, timeout_s=3)
                out["got"] = "returned"
            except Exception as e:  # DaemonTimeout is the right answer; anything else is wrong
                out["got"] = e
            out["dt"] = time.monotonic() - t0

        t0 = time.monotonic()
        worker = threading.Thread(target=shake, daemon=True)
        worker.start()
        worker.join(15)  # the defect blocks forever: never let it hold the script
        if not check(not worker.is_alive(), "handshake(timeout 3 s) is still blocked after 15 s"):
            return
        got = out.get("got")
        check(isinstance(got, transport.DaemonTimeout), f"a trickling peer -> {type(got).__name__}: {got}")
        check(out["dt"] < 4.5, f"handshake(timeout 3 s) returned after {out['dt']:.1f} s")


@contextlib.contextmanager
def health_server() -> Iterator[tuple[str, list[str]]]:
    seen: list[str] = []
    health = {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            body = json.dumps(
                health if self.path == "/api/v0/health" else {**health, "packets_dirs": []}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        t.join(5)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def token_second_line() -> None:
    with plugin_env("token") as base:
        token_file = base / "token.txt"
        token_file.write_text(ROTATED + "\n", encoding="utf-8", newline="\n")
        os.environ["PLATE_TOKEN_FILE"] = str(token_file)
        cap = _Capture()
        logging.getLogger("plate_max").addHandler(cap)
        try:
            with health_server() as (url, seen):
                cfg = {**config.load_config(), "daemon_url": url}
                check(cfg.get("token") is None, "a token file with a second line was loaded as a token")
                try:
                    transport.session(cfg)
                    got: Any = "answered"
                except transport.Unauthorized as e:
                    got = e
                except Exception as e:
                    got = e
                check(
                    isinstance(got, transport.Unauthorized),
                    f"session with that token -> {type(got).__name__}",
                )
                check(SECRET not in str(got), "the refusal shows the token")
                gate = send_packet.daemon_gate(cfg)
                check(
                    not gate.ok and SECRET not in str(gate.reason),
                    f"daemon_gate: ok={gate.ok} {gate.reason!r}",
                )
                check(all(p == "/api/v0/health" for p in seen), f"the bearer left for {seen}")
        finally:
            logging.getLogger("plate_max").removeHandler(cap)
        check(not any(SECRET in line for line in cap.lines), "the token is in the plugin's log")


def hash_503_retry() -> None:
    with plugin_env("hash503") as base:
        packets = base / "packets"
        packets.mkdir()
        daemon = StubDaemon(packets)
        real = daemon.hasher.answer
        left = {"n": 1}

        def flaky(headers: Any, body: Any) -> Any:
            if left["n"]:
                left["n"] -= 1
                daemon.hasher.requests.append(body)
                return 503, {"error": "daemon restarting"}
            return real(headers, body)

        daemon.hasher.answer = flaky
        waits: list[float] = []
        real_sleep = getattr(transport, "_sleep", None)  # the retry's pause, recorded instead of slept
        transport._sleep = waits.append
        try:
            scene, _vray = vray_scene(install)
            with serve_daemon(daemon) as cfg:
                [r] = send(["CAM_front"], packets, cfg)
        finally:
            if real_sleep is None:
                del transport._sleep
            else:
                transport._sleep = real_sleep
        check(r.ok, f"one 503 on /hash failed the send: {r.stage} {r.errors[:1]}")
        check(len(scene.render_calls) == 1, f"{len(scene.render_calls)} renders for one send")
        check(left["n"] == 0 and waits[:1] == [1.0], f"retry waits {waits}")
        note(f"waited {waits} before asking /hash again")


CHECKS = {
    "dup-node-names": dup_node_names,
    "dup-cameras": dup_cameras,
    "nonlatin-names": nonlatin_names,
    "nonfinite-pose": nonfinite_pose,
    "degenerate-lens": degenerate_lens,
    "corrupt-sidecar": corrupt_sidecar,
    "job-guard": job_guard,
    "claimed-job-recovery": claimed_job_recovery,
    "trickle-handshake": trickle_handshake,
    "token-second-line": token_second_line,
    "hash-503-retry": hash_503_retry,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
