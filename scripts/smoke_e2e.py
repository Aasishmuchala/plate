#!/usr/bin/env python3
"""End-to-end smoke: the served daemon driven over HTTP the way the UI drives it, on the fake engine.

    python scripts/smoke_e2e.py [--build-ui] [--port N] [--keep] [--skip-suites]

    --build-ui     build ui/dist first (`npm run build -w ui`); it is built anyway when index.html is missing
    --port N       the daemon's port (default: a free loopback port)
    --keep         keep the smoke home (<repo>/.smoke/<timestamp>/) instead of deleting it at the end
    --skip-suites  skip the UI unit suite and the daemon wiring tests (only while iterating on this script)

It first runs the UI unit suite (`npm test` in ui/) and the daemon wiring tests
(`pytest daemon/tests/test_api_wiring.py`), so this is the one command a verification run cites. Then it makes a
fresh home under `<repo>/.smoke/<timestamp>/`, starts `python -m plate_daemon` on it with `PLATE_HOME`,
`PLATE_PORT`, `PLATE_FAKE_ENGINE=1` (the echo fake), `PLATE_PIXELS_INPROC=1` and `PLATE_NIGHTLY=0`, reads the
token from `<home>/config.json`, waits for `/api/v0/health` and walks one still from packet to shipped export:

    project `smoke` (non-NDA, every grade) -> POST /packets (room_a) -> ingested -> listed with its plate hash
    -> plate thumbnail -> `fake` in /engines -> graph Plate -> Mask(VEG_shrub_A) -> Edit(fake) -> Export
    -> quote (router verdict) -> human approval -> DONE -> gate pass/advisory, identical outside the mask,
    feather band advisory -> full scrub -> accept -> the server marks the Edit approved -> png16 export with
    lineage and `.gate.json`, report shipped -> run history -> rescore writes version 2 -> the built UI at `/`
    with a JavaScript asset -> an unknown API path is a JSON 404

`fixtures/packets/room_a` names project `plate-fixture` and the daemon files a packet under the project its
manifest names, so the smoke sends a copy staged in `<home>/packets/room_a` whose manifest says `smoke` (the
files and their hashes are untouched; the committed fixture is never modified). Client imagery is never read.

The last line is `SMOKE PASS` (exit 0), or `SMOKE FAIL <step>: <detail>` (exit 1) after the daemon log tail.
Standard library only (urllib, subprocess, json); Windows and macOS alike.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
UI_INDEX = UI_DIR / "dist" / "index.html"
FIXTURE = ROOT / "fixtures" / "packets" / "room_a"
SMOKE_DIR = ROOT / ".smoke"
PYTHONPATH_DIRS = ("spec", "daemon", "pixels", "engines", "gates", "plugins/max", "tools")
WIRING_TESTS = "daemon/tests/test_api_wiring.py"

API = "/api/v0"
PROJECT = "smoke"
CUSTODY = {"nda": False, "allowed_grades": ["C0", "C1", "C2", "D"]}
PACKET_VERSION = "0.1"
SENDER = {"name": "smoke_e2e", "version": "1"}
MASK_NAME = "VEG_shrub_A"
EDIT_NODE = "edit"
FEATHER_CHECK = "feather_band.de00_p99"
# What a browser runs as a module script (WHATWG "JavaScript MIME type essence"); text/plain must fail.
JS_TYPES = ("text/javascript", "application/javascript", "application/x-javascript")
IN_FLIGHT = frozenset(
    {"APPROVED", "QUEUED", "STAGING", "SUBMITTED", "RUNNING", "FETCHING", "NORMALIZING", "GATING"}
)

HEALTH_WAIT_S = 90.0
INGEST_WAIT_S = 120.0
RUN_WAIT_S = 240.0
POLL_S = 0.25
REQUEST_TIMEOUT_S = 60.0
SUITE_TIMEOUT_S = 600.0
STOP_WAIT_S = 15.0
LOG_TAIL_LINES = 40
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SCRIPT_RE = re.compile(r"""<script[^>]*\bsrc=["']([^"']+\.js)["']""", re.I)
LINEAGE_CODE = (
    "import json, sys\n"
    "from plate_daemon import lineage\n"
    "found = lineage.read(sys.argv[1])\n"
    "print(json.dumps(found.to_dict() if found is not None else None))\n"
)


class SmokeFail(Exception):
    def __init__(self, step: str, detail: str):
        self.step, self.detail = step, detail
        super().__init__(f"{step}: {detail}")


# ---------------------------------------------------------------------------- HTTP (urllib, no proxies)


@dataclass
class Resp:
    method: str
    path: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError as e:
            raise ValueError(f"{self.method} {self.path}: not JSON ({e}): {self.text()}") from e

    def text(self, limit: int = 400) -> str:
        s = self.body.decode("utf-8", errors="replace")
        return s if len(s) <= limit else s[:limit] + "..."


@dataclass
class Http:
    base: str
    token: str | None = None
    # The daemon is loopback-only: an HTTP(S)_PROXY in the environment must never see these requests.
    opener: urllib.request.OpenerDirector = field(
        default_factory=lambda: urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> Resp:
        hdrs = {"Accept": "*/*"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if auth and self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return Resp(method, path, r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp is not None else b""
            hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
            return Resp(method, path, e.code, hdr, raw)


# ---------------------------------------------------------------------------- small helpers


def _env() -> dict[str, str]:
    """The caller's environment minus every PLATE_* variable (a stray PLATE_TOKEN_FILE or PLATE_DEV would make
    this a different daemon), with the repo's packages on PYTHONPATH and UTF-8 IO."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PLATE_")}
    paths = [str(ROOT / d) for d in PYTHONPATH_DIRS]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_home() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())  # no ':' in a file name (Windows)
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        home = SMOKE_DIR / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            home.mkdir()
        except FileExistsError:
            continue
        (home / "logs").mkdir()
        return home
    raise RuntimeError(f"could not make a fresh smoke home under {SMOKE_DIR}")


def _remove_tree(path: Path, attempts: int = 20) -> str | None:
    """rmtree with retries (Windows keeps a killed process's handles for a moment). None, or why it failed."""
    last = ""
    for _ in range(attempts):
        if not path.exists():
            return None
        try:
            shutil.rmtree(path)
            return None
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.25)
    return last or None


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [ANSI_RE.sub("", ln) for ln in text.splitlines()[-lines:]]


def _summary_line(text: str) -> str:
    """The suite's own verdict line (vitest's `Tests  N passed`, pytest's `N passed in Xs`), else its last line."""
    lines = [ANSI_RE.sub("", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    for pat in (r"^Tests\s+\d+", r"\b\d+ (passed|failed)\b"):
        hits = [ln for ln in lines if re.search(pat, ln)]
        if hits:
            return hits[-1]
    return lines[-1] if lines else "(no output)"


def _wait(
    what: Callable[[], Any], done: Callable[[Any], bool], timeout_s: float, alive: Callable[[], None]
) -> Any:
    """Call `what()` until `done(result)` or `timeout_s` passes (then the last result is returned)."""
    deadline = time.monotonic() + timeout_s
    while True:
        alive()
        got = what()
        if done(got) or time.monotonic() >= deadline:
            return got
        time.sleep(POLL_S)


# ---------------------------------------------------------------------------- the smoke


class Smoke:
    def __init__(self, args: argparse.Namespace, home: Path):
        self.args = args
        self.home = home
        self.env = _env()
        self.step_name = "setup"
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_path = home / "logs" / "daemon.log"
        self._log_file: Any = None
        self.port = 0
        self.http = Http("")

    # ------------------------------------------------------------------ steps and checks

    @contextmanager
    def step(self, name: str) -> Iterator[list[str]]:
        """A named step: `notes` collects what the `ok` line says; any error becomes `SMOKE FAIL <name>`."""
        self.step_name = name
        notes: list[str] = []
        try:
            yield notes
        except SmokeFail:
            raise
        except Exception as e:
            raise SmokeFail(name, f"{type(e).__name__}: {e}") from e
        print(f"ok    {name}" + (f"  {'; '.join(notes)}" if notes else ""), flush=True)

    def fail(self, detail: str) -> SmokeFail:
        return SmokeFail(self.step_name, detail)

    def check(self, cond: Any, detail: str) -> None:
        if not cond:
            raise self.fail(detail)

    def expect(self, resp: Resp, *statuses: int) -> Resp:
        if resp.status not in statuses:
            raise self.fail(f"{resp.method} {resp.path} -> {resp.status}, want {statuses}: {resp.text()}")
        return resp

    def api(self, method: str, path: str, body: Any = None, *, status: int = 200, **kw: Any) -> Any:
        return self.expect(self.http.call(method, API + path, body, **kw), status).json()

    def alive(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            raise self.fail(f"the daemon exited with code {self.proc.returncode}")

    # ------------------------------------------------------------------ suites and the UI build

    def run_cmd(self, argv: list[str], cwd: Path, log_name: str) -> str:
        log = self.home / "logs" / f"{log_name}.log"
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=SUITE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise self.fail(f"{' '.join(argv)} did not finish in {SUITE_TIMEOUT_S:.0f} s") from e
        except OSError as e:
            raise self.fail(f"cannot run {argv[0]}: {e}") from e
        text = done.stdout.decode("utf-8", errors="replace")
        log.write_text(text, encoding="utf-8", newline="\n")
        if done.returncode != 0:
            for ln in [ANSI_RE.sub("", x) for x in text.splitlines()[-LOG_TAIL_LINES:]]:
                print(f"  | {ln}", flush=True)
            raise self.fail(
                f"{' '.join(argv)} exited {done.returncode}: {_summary_line(text)} (log above, {log.name})"
            )
        return _summary_line(text)

    def npm(self) -> str:
        npm = shutil.which("npm")  # npm.cmd on Windows (PATHEXT)
        if npm is None:
            raise self.fail("npm not found on PATH (scripts/bootstrap installs node and the UI deps)")
        return npm

    def suites(self) -> None:
        with self.step("suite.ui") as notes:
            notes.append(self.run_cmd([self.npm(), "test"], UI_DIR, "ui-tests"))
        with self.step("suite.wiring") as notes:
            # pyproject's addopts already says -q; a second -q would drop the "N passed" summary line.
            notes.append(self.run_cmd([sys.executable, "-m", "pytest", WIRING_TESTS], ROOT, "wiring-tests"))

    def build_ui(self) -> None:
        if not self.args.build_ui and UI_INDEX.is_file():
            with self.step("ui.build") as notes:
                notes.append("ui/dist present (pass --build-ui to rebuild)")
            return
        with self.step("ui.build") as notes:
            notes.append(self.run_cmd([self.npm(), "run", "build", "-w", "ui"], ROOT, "ui-build"))
            self.check(UI_INDEX.is_file(), f"the build left no {UI_INDEX.relative_to(ROOT)}")

    # ------------------------------------------------------------------ the daemon

    def start_daemon(self) -> None:
        with self.step("daemon.start") as notes:
            self.port = self.args.port or _free_port()
            env = {
                **self.env,
                "PLATE_HOME": str(self.home),
                "PLATE_PORT": str(self.port),
                "PLATE_FAKE_ENGINE": "1",
                "PLATE_PIXELS_INPROC": "1",
                "PLATE_NIGHTLY": "0",
                "PYTHONUNBUFFERED": "1",
            }
            self._log_file = self.log_path.open("wb")
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "plate_daemon"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
            )
            base = f"http://127.0.0.1:{self.port}"
            probe = Http(base)

            def health() -> Resp | None:
                try:
                    return probe.call("GET", f"{API}/health", auth=False, timeout=5)
                except OSError:  # not listening yet
                    return None

            got = _wait(health, lambda r: r is not None and r.status == 200, HEALTH_WAIT_S, self.alive)
            self.check(got is not None and got.status == 200, f"no /health answer in {HEALTH_WAIT_S:.0f} s")
            h = got.json()
            self.check(h.get("ok") is True, f"/health: {h}")
            self.check(h.get("stub_engine") is True, f"/health says the engine is not the fake: {h}")
            self.check(h.get("pixels") == "inproc", f"/health pixels {h.get('pixels')!r}, want inproc")
            cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
            token = cfg.get("token")
            self.check(isinstance(token, str) and token, "config.json holds no token")
            printed = f"PLATE daemon http://127.0.0.1:{self.port}/#token={token}"
            self.check(
                any(ln.strip() == printed for ln in _tail(self.log_path, 400)),
                "the daemon did not print its UI URL with the config.json token",
            )
            self.http = Http(base, token)
            notes.append(f"{base} daemon {h.get('daemon_version')}, pixels {h.get('pixels')}")

    def stop_daemon(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=STOP_WAIT_S)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------ the walk

    def stage_packet(self) -> Path:
        with self.step("packet.stage") as notes:
            src_manifest = FIXTURE / "manifest.json"
            self.check(src_manifest.is_file(), f"{FIXTURE.relative_to(ROOT)} is missing (fixtures, T2)")
            manifest = json.loads(src_manifest.read_text(encoding="utf-8-sig"))
            if manifest.get("project_id") == PROJECT:
                notes.append(f"{FIXTURE.relative_to(ROOT)} by reference")
                return FIXTURE
            dest = self.home / "packets" / FIXTURE.name
            shutil.copytree(FIXTURE, dest)
            manifest["project_id"] = PROJECT
            (dest / "manifest.json").write_text(
                json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
            notes.append(f"room_a copied with project_id {manifest.get('project_id')!r}")
            return dest

    def walk(self) -> None:
        ui = {"X-Plate-Client": "ui"}

        with self.step("project.create") as notes:
            head = self.api("POST", "/projects", {"project_id": PROJECT, "custody": CUSTODY}, status=201)
            self.check(head.get("project_id") == PROJECT, f"created {head}")
            notes.append(f"custody {head.get('custody')}")

        packet_dir = self.stage_packet()

        with self.step("packet.post") as notes:
            r = self.expect(
                self.http.call(
                    "POST",
                    f"{API}/packets",
                    {"packet_path": str(packet_dir), "sender": SENDER},
                    headers={"X-Plate-Packet-Version": PACKET_VERSION},
                ),
                202,
            ).json()
            packet_id = r.get("packet_id")
            self.check(isinstance(packet_id, str) and packet_id, f"no packet_id in {r}")
            notes.append(f"{packet_id} {r.get('status')}")

        with self.step("packet.ingest") as notes:
            t0 = time.monotonic()
            rec = _wait(
                lambda: self.api("GET", f"/packets/{packet_id}"),
                lambda v: v.get("status") != "ingesting",
                INGEST_WAIT_S,
                self.alive,
            )
            self.check(rec.get("status") == "ingested", f"packet status {rec.get('status')!r}: {rec}")
            notes.append(f"ingested in {time.monotonic() - t0:.1f} s")

        with self.step("packet.list") as notes:
            rows = self.api("GET", f"/projects/{PROJECT}/packets")
            row = next((x for x in rows if x.get("packet_id") == packet_id), None)
            self.check(row is not None, f"/projects/{PROJECT}/packets lacks {packet_id}: {rows}")
            plate = (row.get("plate") or {}).get("blake3")
            self.check(isinstance(plate, str) and plate, f"no plate.blake3 in {row}")
            notes.append(f"plate {plate[:16]}...")

        with self.step("asset.thumb") as notes:
            r = self.expect(self.http.call("GET", f"{API}/assets/{plate}?fmt=thumb"), 200)
            self.check(r.content_type.startswith("image/"), f"thumb content-type {r.content_type!r}")
            self.check(len(r.body) > 0, "empty thumbnail")
            notes.append(f"{r.content_type} {len(r.body)} bytes, source {r.headers.get('x-plate-source')}")

        with self.step("engines.list") as notes:
            engines = self.api("GET", "/engines")
            ids = [e.get("id") for e in engines]
            self.check("fake" in ids, f"/engines lacks fake: {ids}")
            notes.append(f"{len(ids)} engines incl. fake")

        with self.step("packet.summary") as notes:
            summary = self.api("GET", f"/packets/{packet_id}/summary")
            entries = (summary.get("legend") or {}).get("entries") or []
            hits = [e for e in entries if e.get("name") == MASK_NAME]
            hit = next((e for e in hits if e.get("kind") == "object"), hits[0] if hits else None)
            self.check(hit is not None, f"no {MASK_NAME} in the legend of {packet_id}")
            self.check(hit.get("editable") is True, f"{MASK_NAME} is not editable: {hit}")
            mask_id = hit["id"]
            notes.append(f"{MASK_NAME} = {mask_id}")

        graph = {
            "nodes": [
                {"id": "plate", "kind": "Plate", "label": "Plate", "packetId": packet_id, "tier": "A"},
                {"id": "mask", "kind": "Mask", "label": "Mask", "params": {"ids": [mask_id]}},
                {
                    "id": EDIT_NODE,
                    "kind": "Edit",
                    "label": "Edit",
                    "params": {"engine": "fake", "prompt": "smoke"},
                },
                {"id": "export", "kind": "Export", "label": "Export"},
            ],
            "edges": [
                {"id": "plate->mask", "source": "plate", "target": "mask"},
                {"id": "mask->edit", "source": "mask", "target": EDIT_NODE},
                {"id": "edit->export", "source": EDIT_NODE, "target": "export"},
            ],
        }

        with self.step("graph.put") as notes:
            saved = self.api("PUT", f"/projects/{PROJECT}/graph", graph, headers=ui)
            self.check(saved.get("ok") is True, f"graph not saved: {saved}")
            notes.append(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        with self.step("node.quote") as notes:
            q = self.api("POST", f"/projects/{PROJECT}/nodes/{EDIT_NODE}/quote", {"engine_id": "fake"})
            router = q.get("router")
            self.check(isinstance(router, dict) and "verdict" in router, f"no router.verdict: {router}")
            run_id = (q.get("run") or {}).get("id")
            self.check(isinstance(run_id, str) and run_id, f"no run id in the quote: {q.get('run')}")
            self.check(q["run"].get("state") == "QUOTED", f"run state {q['run'].get('state')!r}")
            quote = q.get("quote") or {}
            notes.append(
                f"{run_id} USD {quote.get('price_usd')} grade {quote.get('custody_grade')} "
                f"router {router.get('verdict')}"
            )

        with self.step("run.approve") as notes:
            run = self.api("POST", f"/runs/{run_id}/approve", {"by": "human"})
            self.check(run.get("approved_by") == "human", f"approved_by {run.get('approved_by')!r}")
            notes.append(f"state {run.get('state')}")

        with self.step("run.done") as notes:
            t0 = time.monotonic()
            run = _wait(
                lambda: self.api("GET", f"/runs/{run_id}"),
                lambda v: v.get("state") not in IN_FLIGHT,
                RUN_WAIT_S,
                self.alive,
            )
            self.check(
                run.get("state") == "DONE",
                f"run {run_id} is {run.get('state')}: {run.get('error') or run.get('last_error')}",
            )
            self.check(run.get("exportable") is True, "a DONE run that is not exportable")
            notes.append(f"DONE in {time.monotonic() - t0:.1f} s")

        with self.step("gate.report") as notes:
            rep = self.api("GET", f"/runs/{run_id}/gate")
            self.check(rep.get("verdict") in ("pass", "advisory"), f"gate verdict {rep.get('verdict')!r}")
            outside = (rep.get("metrics") or {}).get("outside_mask") or {}
            self.check(outside.get("identical_outside") is True, f"outside_mask {outside}")
            feather = [c for c in rep.get("checks") or [] if c.get("metric_id") == FEATHER_CHECK]
            self.check(feather, f"no {FEATHER_CHECK} check in the report")
            self.check(feather[0].get("status") == "advisory", f"{FEATHER_CHECK} {feather[0]}")
            self.check(rep.get("n") == 1, f"report n {rep.get('n')!r}, want 1")
            notes.append(f"verdict {rep.get('verdict')}, set {rep.get('threshold_set_id')}")

        with self.step("run.scrub") as notes:
            scrub = self.api("POST", f"/runs/{run_id}/scrub", {"quadrants_visited": 4, "zoom_100": True})
            self.check(scrub.get("full_scrub") is True, f"not a full scrub: {scrub}")
            scrub_id = scrub.get("scrub_log_id")
            self.check(isinstance(scrub_id, str) and scrub_id, f"no scrub_log_id: {scrub}")
            notes.append(scrub_id)

        with self.step("run.accept") as notes:
            run = self.api("POST", f"/runs/{run_id}/accept", {"scrub_log_id": scrub_id})
            accepted = run.get("accepted")
            self.check(isinstance(accepted, dict), f"no accept note on the run: {accepted!r}")
            notes.append(f"accepted by {accepted.get('by') or 'human'}")

        with self.step("graph.approved") as notes:
            saved = self.api("PUT", f"/projects/{PROJECT}/graph", graph, headers=ui)
            node = next((n for n in saved["graph"]["nodes"] if n.get("id") == EDIT_NODE), {})
            self.check(node.get("approved") is True, f"PUT answered the Edit node as {node}")
            stored = self.api("GET", f"/projects/{PROJECT}/graph")
            node = next((n for n in stored["nodes"] if n.get("id") == EDIT_NODE), {})
            self.check(node.get("approved") is True, f"the stored Edit node is {node}")
            self.check(node.get("scrubLogId") == scrub_id, f"stored scrubLogId {node.get('scrubLogId')!r}")
            notes.append(f"server-derived approved, gateStatus {node.get('gateStatus')}")

        out_dir = self.home / "out"
        with self.step("run.export") as notes:
            ex = self.api("POST", f"/runs/{run_id}/export", {"out_dir": str(out_dir), "format": "png16"})
            path, sidecar = Path(ex.get("path") or ""), Path(ex.get("sidecar") or "")
            self.check(path.is_file() and path.parent == out_dir, f"no exported file at {path}")
            self.check(sidecar.is_file(), f"no sidecar at {sidecar}")
            self.check(sidecar.name.endswith(".gate.json"), f"sidecar name {sidecar.name}")
            json.loads(sidecar.read_text(encoding="utf-8"))
            notes.append(f"{path.name} + {sidecar.name}")

        with self.step("export.lineage") as notes:
            got = subprocess.run(
                [sys.executable, "-c", LINEAGE_CODE, str(path)],
                cwd=ROOT,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
            )
            out = got.stdout.decode("utf-8", errors="replace").strip().splitlines()
            err = got.stderr.decode("utf-8", errors="replace").strip()
            self.check(got.returncode == 0 and out, f"lineage.read failed ({got.returncode}): {err[-400:]}")
            lin = json.loads(out[-1])
            self.check(isinstance(lin, dict), f"no lineage in {path.name}")
            self.check(lin.get("run_id") == run_id, f"lineage names run {lin.get('run_id')!r}")
            self.check(lin.get("project_id") == PROJECT, f"lineage names project {lin.get('project_id')!r}")
            notes.append(f"run {lin['run_id']}, packet {lin.get('packet_id')}, engine {lin.get('engine_id')}")

        with self.step("gate.shipped"):
            rep = self.api("GET", f"/runs/{run_id}/gate")
            self.check(
                rep.get("shipped") is True, f"the report is not shipped: shipped={rep.get('shipped')!r}"
            )

        with self.step("runs.list") as notes:
            runs = self.api("GET", f"/projects/{PROJECT}/runs")
            self.check(any(r.get("id") == run_id for r in runs), f"/projects/{PROJECT}/runs lacks {run_id}")
            notes.append(f"{len(runs)} run(s)")

        with self.step("gate.rescore") as notes:
            rep = self.api("POST", f"/runs/{run_id}/gate/rescore", {})
            self.check(rep.get("n") == 2, f"rescore n {rep.get('n')!r}, want 2")
            notes.append(f"versions {rep.get('versions')}, verdict {rep.get('verdict')}")

        with self.step("ui.index") as notes:
            r = self.expect(self.http.call("GET", "/", auth=False), 200)
            html = r.body.decode("utf-8", errors="replace")
            self.check(r.content_type == "text/html", f"/ content-type {r.content_type!r}")
            self.check('<div id="root"' in html, '/ has no <div id="root"')
            scripts = [s for s in SCRIPT_RE.findall(html) if s.startswith("/assets/")]
            self.check(scripts, "/ names no built /assets/*.js")
            notes.append(f"index.html -> {scripts[0]}")

        with self.step("ui.asset") as notes:
            r = self.expect(self.http.call("GET", scripts[0], auth=False), 200)
            self.check(r.content_type in JS_TYPES, f"{scripts[0]} content-type {r.content_type!r}")
            notes.append(f"{r.content_type}, {len(r.body)} bytes")

        with self.step("api.404") as notes:
            r = self.expect(self.http.call("POST", f"{API}/nope", {}), 404)
            self.check(r.content_type == "application/json", f"404 content-type {r.content_type!r}")
            self.check(r.json().get("error") == "not_found", f"404 body {r.text()}")
            notes.append(r.text(120))

    def run(self) -> None:
        if not self.args.skip_suites:
            self.suites()
        self.build_ui()
        self.start_daemon()
        self.walk()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end smoke of the served daemon on the fake engine.")
    ap.add_argument("--build-ui", action="store_true", help="build ui/dist first (npm run build -w ui)")
    ap.add_argument("--port", type=int, default=0, help="the daemon's port (default: a free loopback port)")
    ap.add_argument("--keep", action="store_true", help="keep <repo>/.smoke/<timestamp>/ at the end")
    ap.add_argument(
        "--skip-suites", action="store_true", help="skip the UI unit suite and the daemon wiring tests"
    )
    args = ap.parse_args(argv)
    if args.port and not 1 <= args.port <= 65535:
        ap.error(f"--port {args.port} out of range")

    for stream in (sys.stdout, sys.stderr):  # a cp1252 pipe on Windows must not crash on a tool's check mark
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    home = _make_home()
    print(f"smoke home {home}", flush=True)
    smoke = Smoke(args, home)
    failure: SmokeFail | None = None
    tail: list[str] = []
    try:
        smoke.run()
    except SmokeFail as e:
        failure = e
    except KeyboardInterrupt:
        failure = SmokeFail(smoke.step_name, "interrupted")
    finally:
        smoke.stop_daemon()
    if failure is not None:
        tail = _tail(smoke.log_path)
    if args.keep:
        print(f"kept {home}", flush=True)
    else:
        problem = _remove_tree(home)
        if problem:
            print(f"warning: could not delete {home}: {problem}", flush=True)
        try:
            SMOKE_DIR.rmdir()  # only when no other smoke home is left in it
        except OSError:
            pass
    if failure is not None:
        if tail:
            print(f"--- daemon log tail ({len(tail)} lines) ---")
            for ln in tail:
                print(f"  | {ln}")
            print("--- end of daemon log ---")
        print(f"SMOKE FAIL {failure.step}: {failure.detail}", flush=True)
        return 1
    print("SMOKE PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
