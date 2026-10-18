"""Shared harness for the stress scripts (`scripts/stress/*.py`): a throwaway daemon, its clients, the verdicts.

Standard library plus httpx only. Every stress script turns one attacker's repro (router/PLAN.md Wave 12) into an
assertion of the fixed behaviour, talks to the daemon over HTTP and never imports the daemon package (the two
named exceptions, `max_plugin` and `mcp_cli_imports`, import what their attackers imported).

    with Daemon(name="api_fuzz") as d:            # fresh home under <repo>/.stress/<timestamp>/<name>/
        d.client.get("/health")                   # bearer token, no X-Plate-Client: an agent or a script
        d.ui_client.put(...)                      # the same plus X-Plate-Client: ui (the canvas)
        d.kill()                                  # SIGKILL (TerminateProcess on Windows)
        d.restart()                               # same home, same port, same environment

`Daemon` starts `python -m plate_daemon` with `PLATE_HOME`, `PLATE_PORT` (a free loopback port unless given),
`PLATE_FAKE_ENGINE` (`1`, or the JSON flags a script passes), `PLATE_PIXELS_INPROC=1` and `PLATE_NIGHTLY=0`, every
other `PLATE_*` variable of the caller's environment removed, reads the token from `<home>/config.json` and waits
for `/api/v0/health`. `main(CHECKS)` runs the checks in order and prints one line each (`ok`, `skip` or
`FINDING`), then `<script>: N checks, N ok, N skipped, N findings`, and exits 1 when a finding stands. The homes
of a clean run are deleted unless `--keep`; a run with a finding keeps them for the post-mortem.

`PLATE_STRESS_SRC=<folder>` puts another source tree's packages (for example `git archive 1f272f2` unpacked in a
scratch folder) ahead of this checkout's, for the daemon and for the two in-process scripts, so a check can be
shown to fail on the code it was written against.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
STRESS_DIR = ROOT / ".stress"
FIXTURES = ROOT / "fixtures" / "packets"
PYTHONPATH_DIRS = ("spec", "daemon", "pixels", "engines", "gates", "plugins/max", "tools")
API = "/api/v0"
PACKET_HEADERS = {"X-Plate-Packet-Version": "0.1"}
SENDER = {"name": "stress", "version": "1"}
UI = {"X-Plate-Client": "ui"}
ALL_GRADES = ["C0", "C1", "C2", "D"]
IN_FLIGHT = frozenset(
    {"APPROVED", "QUEUED", "STAGING", "SUBMITTED", "RUNNING", "FETCHING", "NORMALIZING", "GATING"}
)
HEALTH_WAIT_S = 90.0
STOP_WAIT_S = 15.0
REQUEST_TIMEOUT_S = 60.0


def source_root() -> Path:
    """The source tree whose packages run: this checkout, or `PLATE_STRESS_SRC`."""
    src = os.environ.get("PLATE_STRESS_SRC")
    return Path(src).resolve() if src else ROOT


def source_paths() -> list[str]:
    root = source_root()
    paths = [str(root / d) for d in PYTHONPATH_DIRS]
    if root != ROOT:
        paths += [str(ROOT / d) for d in PYTHONPATH_DIRS]
    return paths


def use_source_tree() -> None:
    """Put the source tree's packages first on this process's sys.path (the two in-process scripts)."""
    for p in reversed(source_paths()):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's environment minus every PLATE_* variable, the packages on PYTHONPATH, UTF-8 IO."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PLATE_")}
    paths = source_paths()
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"})
    env.update(extra or {})
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def remove_tree(path: Path, attempts: int = 20) -> None:
    """rmtree with retries (Windows keeps a killed process's handles for a moment) and read-only files fixed."""

    def unlock(func: Callable[..., Any], p: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)
            parent = os.path.dirname(p)
            os.chmod(parent, 0o700)
        func(p)

    for _ in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onexc=unlock)
            return
        except OSError:
            time.sleep(0.25)


# ---------------------------------------------------------------------------- the run: stamp, homes, verdicts


class Skip(Exception):
    """A check that cannot run here (no RAM disk, Windows, absent tool): reported, never a finding."""


class Run:
    """One script run: its options, the homes it made and the findings so far."""

    def __init__(self) -> None:
        self.script = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "stress"
        self.keep = False
        self.homes: list[Path] = []
        self.findings: list[tuple[str, str]] = []
        self.notes: dict[str, list[str]] = {}
        self.current = ""
        self._lock = threading.Lock()

    def stamp_dir(self) -> Path:
        stamp = os.environ.get("PLATE_STRESS_STAMP") or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        os.environ["PLATE_STRESS_STAMP"] = stamp  # the next home of this run lands beside the first
        STRESS_DIR.mkdir(parents=True, exist_ok=True)
        ignore = STRESS_DIR / ".gitignore"
        if not ignore.exists():  # runtime state, never committed: git skips the whole folder
            ignore.write_text("*\n", encoding="utf-8", newline="\n")
        return STRESS_DIR / stamp

    def new_home(self, name: str | None = None) -> Path:
        base = name or f"{self.script}-{self.current or 'home'}"
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in base)[:60]
        parent = self.stamp_dir()
        parent.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            home = parent / (safe if n == 1 else f"{safe}-{n}")
            try:
                home.mkdir()
            except FileExistsError:
                continue
            self.homes.append(home)
            return home
        raise RuntimeError(f"could not make a fresh home under {parent}")

    def scratch(self, name: str) -> Path:
        """A scratch folder of this run (deleted with the homes)."""
        return self.new_home(f"{self.script}-{name}")


RUN = Run()
atexit.register(lambda: Daemon.stop_all())  # never leave a daemon behind, however the script ends


def _ended_by_signal(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)  # unwinds through main's finally and atexit: the daemons are stopped


for _name in ("SIGTERM", "SIGHUP", "SIGALRM"):  # a CI cancel, a closed terminal, `perl -e 'alarm ...'`
    if hasattr(signal, _name):
        with contextlib.suppress(ValueError, OSError):  # not the main thread, or not settable on this OS
            signal.signal(getattr(signal, _name), _ended_by_signal)


def finding(name: str, detail: str) -> None:
    """Record a failure: the check `name` saw the attacker's defect again (or could not prove the fix)."""
    with RUN._lock:
        RUN.findings.append((name, detail))


def check(cond: Any, detail: str, name: str | None = None) -> bool:
    """`finding(name or the running check, detail)` unless `cond`; returns bool(cond)."""
    if not cond:
        finding(name or RUN.current, detail)
    return bool(cond)


def skip(reason: str) -> None:
    raise Skip(reason)


def note(text: str) -> None:
    """A measurement or a path taken, printed on the check's `ok` line (recorded, not asserted)."""
    with RUN._lock:
        RUN.notes.setdefault(RUN.current, []).append(text)


def main(checks: Mapping[str, Callable[[], Any]] | Iterable[tuple[str, Callable[[], Any]]]) -> int:
    """Run the checks (all, or `--only`), one line each, a summary line; exit status 1 on any finding."""
    items = list(checks.items()) if isinstance(checks, Mapping) else list(checks)
    ap = argparse.ArgumentParser(description=(sys.modules["__main__"].__doc__ or "").split("\n\n")[0])
    ap.add_argument(
        "--keep", action="store_true", help="keep the daemon homes under .stress/ after a clean run"
    )
    ap.add_argument(
        "--only", action="append", default=[], help="run only this check (repeat, or comma-separate)"
    )
    ap.add_argument("--list", action="store_true", help="print the check names and exit")
    args = ap.parse_args()
    if args.list:
        for name, _ in items:
            print(name)
        return 0
    RUN.keep = args.keep
    only = {n.strip() for arg in args.only for n in arg.split(",") if n.strip()}
    unknown = only - {n for n, _ in items}
    if unknown:
        print(f"{RUN.script}: unknown check(s) {sorted(unknown)}; known: {[n for n, _ in items]}")
        return 2
    t_all = time.monotonic()
    ran = ok = skipped = 0
    try:
        for name, fn in items:
            if only and name not in only:
                continue
            ran += 1
            RUN.current = name
            before = len(RUN.findings)
            t0 = time.monotonic()
            skipped_why = None
            try:
                fn()
            except Skip as e:
                skipped_why = str(e)
            except Exception as e:  # the harness could not prove the fix: that is a finding too
                tb = traceback.format_exc(limit=6).strip().splitlines()
                finding(name, f"check raised {type(e).__name__}: {e} | {' / '.join(tb[-4:])}")
            dt = time.monotonic() - t0
            mine = RUN.findings[before:]
            if mine:  # a finding recorded before a skip still stands
                for _, detail in mine:
                    print(f"FINDING {name}: {detail}", flush=True)
            elif skipped_why is not None:
                skipped += 1
                print(f"skip    {name}: {skipped_why}", flush=True)
            else:
                ok += 1
                extra = "; ".join(RUN.notes.get(name, []))
                print(f"ok      {name}  ({dt:.1f} s)" + (f"  {extra}" if extra else ""), flush=True)
    finally:
        Daemon.stop_all()
    n_find = len({n for n, _ in RUN.findings})
    secs = time.monotonic() - t_all
    print(
        f"{RUN.script}: {ran} checks, {ok} ok, {skipped} skipped, {n_find} findings ({secs:.0f} s)",
        flush=True,
    )
    if not RUN.findings and not RUN.keep:
        for home in RUN.homes:
            remove_tree(home)
        stamp = STRESS_DIR / os.environ.get("PLATE_STRESS_STAMP", "")
        with contextlib.suppress(OSError):
            if stamp != STRESS_DIR and stamp.is_dir() and not any(stamp.iterdir()):
                stamp.rmdir()
    elif RUN.homes:
        print(f"{RUN.script}: homes kept under {RUN.homes[0].parent}", flush=True)
    return 1 if RUN.findings else 0


# ---------------------------------------------------------------------------- the daemon


class Daemon:
    """A throwaway `python -m plate_daemon` on its own home and loopback port (a context manager)."""

    live: list[Daemon] = []

    def __init__(
        self,
        home: Path | None = None,
        port: int | None = None,
        env: Mapping[str, str] | None = None,
        *,
        name: str | None = None,
        fake: str | dict[str, Any] | None = None,
    ) -> None:
        self.home = home if home is not None else RUN.new_home(name)
        self.home.mkdir(parents=True, exist_ok=True)
        self.port = port or free_port()
        fake_env = "1" if fake is None else (fake if isinstance(fake, str) else json.dumps(fake))
        self.env: dict[str, str] = {
            "PLATE_FAKE_ENGINE": fake_env,
            "PLATE_PIXELS_INPROC": "1",
            "PLATE_NIGHTLY": "0",
            "PLATE_LABELS": str(self.home / "labels.jsonl"),
            **(env or {}),
        }
        self.proc: subprocess.Popen[bytes] | None = None
        self.token = ""
        self.base = f"http://127.0.0.1:{self.port}"
        self.client: httpx.Client = httpx.Client()
        self.ui_client: httpx.Client = httpx.Client()
        self._log: Any = None
        self.starts = 0

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Daemon:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def log_path(self) -> Path:
        return self.home / f"daemon.{self.starts}.log"

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc is not None else -1

    def start(self, env: Mapping[str, str] | None = None, wait: bool = True) -> Daemon:
        if env:
            self.env.update(env)
        self.starts += 1
        full = child_env({**self.env, "PLATE_HOME": str(self.home), "PLATE_PORT": str(self.port)})
        self._log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "plate_daemon"],
            cwd=ROOT,
            env=full,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        if self not in Daemon.live:
            Daemon.live.append(self)
        if wait:
            self.wait_healthy()
        return self

    def wait_healthy(self, timeout_s: float = HEALTH_WAIT_S) -> None:
        deadline = time.monotonic() + timeout_s
        probe = httpx.Client(base_url=self.base + API, timeout=5, trust_env=False)
        try:
            while True:
                if self.proc is not None and self.proc.poll() is not None:
                    raise RuntimeError(
                        f"the daemon exited with code {self.proc.returncode}: {' | '.join(self.log_tail(8))}"
                    )
                try:
                    if probe.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"no /health answer in {timeout_s:.0f} s: {' | '.join(self.log_tail(8))}"
                    )
                time.sleep(0.2)
        finally:
            probe.close()
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
        self.token = str(cfg["token"])
        self.client.close()
        self.ui_client.close()
        self.client = self.make_client()
        self.ui_client = self.make_client(ui=True)

    def make_client(self, ui: bool = False, timeout: float = REQUEST_TIMEOUT_S, **kw: Any) -> httpx.Client:
        headers = {"Authorization": f"Bearer {self.token}"}
        if ui:
            headers.update(UI)
        # No keep-alive: a daemon that dies inside a request (the defects these scripts look for) closes the
        # socket, and a pooled connection would turn the next check's request into a ReadError.
        kw.setdefault("limits", httpx.Limits(max_keepalive_connections=0, max_connections=200))
        return httpx.Client(base_url=self.base + API, headers=headers, timeout=timeout, trust_env=False, **kw)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self, sig: int | None = None) -> None:
        """Kill hard (SIGKILL by default; Windows has only TerminateProcess) and reap the process."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if sig is None or sys.platform == "win32":
                self.proc.kill()
            else:
                os.kill(self.proc.pid, sig)
            try:
                self.proc.wait(timeout=STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=STOP_WAIT_S)
        self._close_log()

    def stop(self) -> None:
        """Stop gracefully (SIGTERM / terminate), then hard after STOP_WAIT_S."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=STOP_WAIT_S)
        self._close_log()
        self.client.close()
        self.ui_client.close()
        if self in Daemon.live:
            Daemon.live.remove(self)

    def restart(self, env: Mapping[str, str] | None = None, *, hard: bool = False) -> Daemon:
        if hard:
            self.kill()
        else:
            proc = self.proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_WAIT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=STOP_WAIT_S)
            self._close_log()
        return self.start(env)

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    @classmethod
    def stop_all(cls) -> None:
        for d in list(cls.live):
            with contextlib.suppress(Exception):
                d.stop()

    def log_tail(self, lines: int = 20) -> list[str]:
        try:
            text = self.log_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return []
        return text.splitlines()[-lines:]

    def log_text(self) -> str:
        out = []
        for n in range(1, self.starts + 1):
            with contextlib.suppress(OSError):
                out.append((self.home / f"daemon.{n}.log").read_bytes().decode("utf-8", errors="replace"))
        return "\n".join(out)

    # ------------------------------------------------------------------ HTTP sugar

    def api(self, method: str, path: str, body: Any = None, *, ui: bool = False, **kw: Any) -> httpx.Response:
        c = self.ui_client if ui else self.client
        if body is not None:
            kw["json"] = body
        return c.request(method, path, **kw)

    def health_ms(self, timeout: float = 30.0) -> float:
        """Wall time of one GET /health (no token), in ms; inf when it does not answer in `timeout`."""
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as c:
                c.get(self.base + API + "/health")
        except httpx.HTTPError:
            return float("inf")
        return (time.monotonic() - t0) * 1000.0

    # ------------------------------------------------------------------ projects, packets, graphs, runs

    def project(
        self, project_id: str, *, nda: bool = False, grades: list[str] | None = None
    ) -> httpx.Response:
        custody = {"nda": nda, "allowed_grades": grades or (["C0", "C1"] if nda else ALL_GRADES)}
        return self.api("POST", "/projects", {"project_id": project_id, "custody": custody})

    def stage_packet(self, project_id: str, fixture: str = "room_a", name: str | None = None) -> Path:
        """Copy a committed fixture packet into <home>/packets/<name> with its manifest naming `project_id`."""
        dest = self.home / "packets" / (name or f"{fixture}_{project_id}")
        if not dest.exists():
            shutil.copytree(FIXTURES / fixture, dest)
            m = json.loads((dest / "manifest.json").read_text(encoding="utf-8-sig"))
            old, new = str(m["packet_id"]), packet_id_for(str(m["packet_id"]), dest.name)
            m["project_id"], m["packet_id"] = project_id, new  # packet ids are unique per home
            # The legend names the packet too, so it is rewritten and rehashed (BLAKE3 by the daemon, 5b).
            legend = dest / "id_legend.json"
            data = legend.read_bytes().replace(old.encode("ascii"), new.encode("ascii"))
            legend.write_bytes(data)
            entry = m["files"]["id_legend"]
            entry["bytes"], entry["hash"]["hex"] = len(data), self.blake3(data)
            (dest / "manifest.json").write_text(
                json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
        return dest

    def blake3(self, data: bytes) -> str:
        """BLAKE3 hex of `data`, from the daemon's `POST /hash` (the standard library has no BLAKE3)."""
        r = self.api("POST", "/hash", {"payloads": {"x": base64.b64encode(data).decode("ascii")}})
        r.raise_for_status()
        return str(r.json()["hashes"]["x"]["hex"])

    def post_packet(self, path: Path, timeout_s: float = 180.0) -> dict[str, Any]:
        r = self.api("POST", "/packets", {"packet_path": str(path), "sender": SENDER}, headers=PACKET_HEADERS)
        if r.status_code not in (200, 202):
            raise RuntimeError(f"POST /packets {path.name} -> {r.status_code} {r.text[:300]}")
        pid = r.json()["packet_id"]
        deadline = time.monotonic() + timeout_s
        while True:
            rec = self.api("GET", f"/packets/{pid}").json()
            if rec.get("status") != "ingesting" or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        if rec.get("status") != "ingested":
            raise RuntimeError(f"packet {pid} ended {rec.get('status')}: {str(rec)[:300]}")
        return rec

    def mask_id(self, packet_id: str, name: str = "VEG_shrub_A") -> str:
        summ = self.api("GET", f"/packets/{packet_id}/summary").json()
        hits = [e for e in summ["legend"]["entries"] if e["name"] == name]
        hit = next((e for e in hits if e.get("kind") == "object"), hits[0])
        return str(hit["id"])

    def edit_project(
        self, project_id: str, engine: str = "fake", *, nda: bool = False, prompt: str = "stress"
    ) -> tuple[str, str]:
        """Project + room_a packet + Plate -> Mask(VEG_shrub_A) -> Edit(engine) -> Export. (packet_id, mask_id)"""
        r = self.project(project_id, nda=nda)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"POST /projects {project_id} -> {r.status_code} {r.text[:300]}")
        rec = self.post_packet(self.stage_packet(project_id))
        pid = rec.get("packet_id") or rec.get("id")
        mask = self.mask_id(pid)
        g = edit_graph(pid, mask, engine, prompt)
        r = self.api("PUT", f"/projects/{project_id}/graph", g, ui=True)
        if r.status_code != 200:
            raise RuntimeError(f"PUT graph {project_id} -> {r.status_code} {r.text[:300]}")
        return pid, mask

    def quote(self, project_id: str, node: str = "edit", **body: Any) -> httpx.Response:
        body.setdefault("engine_id", "fake")
        return self.api("POST", f"/projects/{project_id}/nodes/{node}/quote", body)

    def quote_run(self, project_id: str, node: str = "edit", **body: Any) -> str:
        r = self.quote(project_id, node, **body)
        if r.status_code != 200:
            raise RuntimeError(f"quote {project_id}/{node} -> {r.status_code} {r.text[:300]}")
        return str(r.json()["run"]["id"])

    def approve(self, run_id: str, by: str = "human", **body: Any) -> httpx.Response:
        return self.api("POST", f"/runs/{run_id}/approve", {"by": by, **body})

    def run(self, run_id: str) -> dict[str, Any]:
        return self.api("GET", f"/runs/{run_id}").json()

    def wait_run(
        self, run_id: str, timeout_s: float = 120.0, until: Callable[[dict[str, Any]], bool] | None = None
    ) -> dict[str, Any]:
        done = until or (lambda r: r.get("state") not in IN_FLIGHT)
        deadline = time.monotonic() + timeout_s
        while True:
            r = self.run(run_id)
            if done(r) or time.monotonic() > deadline:
                return r
            time.sleep(0.25)

    def done_run(self, project_id: str, node: str = "edit", **body: Any) -> str:
        """Quote, approve as a human, wait: the run id of a DONE take (raises otherwise)."""
        rid = self.quote_run(project_id, node, **body)
        a = self.approve(rid)
        if a.status_code != 200:
            raise RuntimeError(f"approve {rid} -> {a.status_code} {a.text[:300]}")
        r = self.wait_run(rid)
        if r.get("state") != "DONE":
            raise RuntimeError(f"run {rid} ended {r.get('state')}: {r.get('reason_code')} {r.get('error')}")
        return rid

    def journal(self) -> dict[str, Any]:
        """The in-process fake provider's journal (`<home>/fake_engine.json`): jobs, polls, call counters."""
        try:
            return json.loads((self.home / "fake_engine.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def index_db(self, project_id: str) -> Path:
        return self.home / "index" / f"{project_id}.sqlite"

    def drop_index(self, project_id: str) -> None:
        """Stop, delete the project's SQLite index (a cache, Decision 6) and start again: boot rebuilds it."""
        self.stop()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.index_db(project_id)) + suffix)
            for _ in range(20):
                try:
                    path.unlink(missing_ok=True)
                    break
                except PermissionError:  # Windows: the killed process's handle is still closing
                    time.sleep(0.25)
        self.start()

    def run_states(self, project_id: str) -> dict[str, str]:
        rows = self.api("GET", f"/projects/{project_id}/runs", params={"limit": 1000}).json()
        return {r["id"]: r["state"] for r in rows}

    def ledger(self, project_id: str) -> list[dict[str, Any]]:
        path = self.home / "projects" / project_id / "runs.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def packet_id_for(packet_id: str, salt: str) -> str:
    """The fixture's packet id with its random half replaced by one derived from `salt` (a ULID shape)."""
    digest = zlib.crc32(salt.encode("utf-8")) | (zlib.adler32(salt.encode("utf-8")) << 32)
    tail = "".join(CROCKFORD[(digest >> (5 * i)) & 31] for i in range(13))
    return packet_id[: len(packet_id) - 13] + tail


def edit_graph(packet_id: str, mask_id: str, engine: str = "fake", prompt: str = "stress") -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "plate", "kind": "Plate", "label": "Plate", "packetId": packet_id, "tier": "A"},
            {"id": "mask", "kind": "Mask", "label": "Mask", "params": {"ids": [mask_id]}},
            {"id": "edit", "kind": "Edit", "label": "Edit", "params": {"engine": engine, "prompt": prompt}},
            {"id": "export", "kind": "Export", "label": "Export"},
        ],
        "edges": [
            {"id": "a", "source": "plate", "target": "mask"},
            {"id": "b", "source": "mask", "target": "edit"},
            {"id": "c", "source": "edit", "target": "export"},
        ],
    }


# ---------------------------------------------------------------------------- bytes the attackers sent


def png(width: int, height: int, rgb: tuple[int, ...] = (128, 128, 128), *, grey: bool = False) -> bytes:
    """A flat 8-bit PNG (RGB, or grey), built row by row so a huge declared size costs little memory here."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    colour = 0 if grey else 2
    px = bytes(rgb[:1]) if grey else bytes(rgb[:3])
    row = b"\x00" + px * width
    z = zlib.compressobj(9)
    idat = b"".join(z.compress(row) for _ in range(height)) + z.flush()
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


HOLD_LOCK = """
import sys, time
import portalocker
lock = portalocker.Lock(sys.argv[1], mode="a", timeout=30, fail_when_locked=False)
lock.acquire()
print("held", flush=True)
time.sleep(float(sys.argv[2]))
lock.release()
print("released", flush=True)
"""


def hold_project_lock(project_dir: Path, seconds: float) -> subprocess.Popen[str]:
    """Hold `<project_dir>/project.lock` for `seconds` from a helper process, as `plate reindex` or `plate gc`
    on a big project does. The helper uses portalocker (the daemon's lock library), not the daemon package.
    Returns once the lock is held."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(Path(project_dir) / "project.lock"), str(seconds)],
        env=child_env(),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    if line != "held":
        proc.kill()
        raise RuntimeError(f"the lock helper did not take the lock: {line!r}")
    return proc


def concurrently(fn: Callable[[int], Any], n: int, *, timeout_s: float = 300.0) -> list[Any]:
    """Call fn(0..n-1) on n threads released together; results in index order (an exception is a result)."""
    out: list[Any] = [None] * n
    bar = threading.Barrier(n)

    def go(i: int) -> None:
        try:
            bar.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        try:
            out[i] = fn(i)
        except Exception as e:
            out[i] = e

    ts = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(n)]
    for t in ts:
        t.start()
    deadline = time.monotonic() + timeout_s
    for t in ts:
        t.join(max(0.0, deadline - time.monotonic()))
    return out


def status_of(r: Any) -> Any:
    return r.status_code if isinstance(r, httpx.Response) else type(r).__name__


def error_of(r: httpx.Response) -> Any:
    try:
        j = r.json()
    except ValueError:
        return None
    return j.get("error") if isinstance(j, dict) else None


def is_json(r: httpx.Response) -> bool:
    return r.headers.get("content-type", "").split(";")[0].strip() == "application/json"


def sigkill() -> int | None:
    return getattr(signal, "SIGKILL", None)
