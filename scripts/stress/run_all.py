#!/usr/bin/env python3
"""Run every stress script (`make stress`, `just stress`): the attackers' regression suite, opt-in.

    python scripts/stress/run_all.py [--keep] [--only <script or check>] [--list]

Each script runs as its own subprocess, in turn, and starts its own daemon(s) on free loopback ports; their
homes share one `<repo>/.stress/<timestamp>/` folder. `--only` takes a script name (`api_fuzz` or `api-fuzz`)
or a check name, which is forwarded to the scripts that define it; `--keep` is forwarded to every script. The
table at the end has one line per script, then `stress: N scripts, N checks, N ok, N skipped, N findings`, and the
exit status is 1 when any script found something (or did not finish). See docs/stress.md.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPTS = (
    "api_fuzz",
    "doctrine",
    "money",
    "crash",
    "engine_faults",
    "scale",
    "ui",
    "max_plugin",
    "mcp_cli_imports",
)
SCRIPT_TIMEOUT_S = 1800.0
SUMMARY_RE = re.compile(
    r"^(?P<script>\w+): (?P<checks>\d+) checks, (?P<ok>\d+) ok, (?P<skipped>\d+) skipped, "
    r"(?P<findings>\d+) findings \((?P<secs>\d+) s\)$"
)


def checks_of(script: str, env: dict[str, str]) -> list[str]:
    done = subprocess.run(
        [sys.executable, str(HERE / f"{script}.py"), "--list"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    return [ln.strip() for ln in done.stdout.splitlines() if ln.strip()] if done.returncode == 0 else []


CURRENT: list[subprocess.Popen[str]] = []  # the script running now, ended with this process on a signal


def _ended_by_signal(signum: int, _frame: object) -> None:
    for proc in CURRENT:
        if proc.poll() is None:
            proc.terminate()  # the script's own handler stops its daemons
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    raise SystemExit(128 + signum)


def run_one(script: str, extra: list[str], env: dict[str, str]) -> dict[str, object]:
    """Run one script, echoing its lines as they come; its summary line, or what went wrong."""
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(HERE / f"{script}.py"), *extra],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    CURRENT[:] = [proc]
    summary: dict[str, object] = {}
    watchdog = threading.Timer(SCRIPT_TIMEOUT_S, proc.terminate)  # a script that hangs silently is ended too
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"  {line.rstrip()}", flush=True)
            m = SUMMARY_RE.match(line.strip())
            if m and m.group("script") == script:
                summary = {k: int(v) for k, v in m.groupdict().items() if k != "script"}
        code = proc.wait()
    finally:
        watchdog.cancel()
    return {"script": script, "code": code, "secs": time.monotonic() - t0, **summary}


def main() -> int:
    for name in ("SIGTERM", "SIGHUP", "SIGALRM"):  # a CI cancel, a closed terminal, `perl -e 'alarm ...'`
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _ended_by_signal)
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--keep", action="store_true", help="keep every daemon home under .stress/")
    ap.add_argument(
        "--only", action="append", default=[], help="a script or a check (repeat, or comma-separate)"
    )
    ap.add_argument("--list", action="store_true", help="print each script's checks and exit")
    args = ap.parse_args()
    env = dict(os.environ)
    env.setdefault("PLATE_STRESS_STAMP", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if args.list:
        for script in SCRIPTS:
            print(f"{script}: {', '.join(checks_of(script, env))}")
        return 0
    wanted = {w.strip() for arg in args.only for w in arg.split(",") if w.strip()}
    plan: list[tuple[str, list[str]]] = []
    if not wanted:
        plan = [(s, []) for s in SCRIPTS]
    else:
        scripts = {w.replace("-", "_") for w in wanted} & set(SCRIPTS)
        checks = {w for w in wanted if w.replace("-", "_") not in SCRIPTS}
        for script in SCRIPTS:
            mine = sorted(checks & set(checks_of(script, env))) if checks else []
            if script in scripts:
                plan.append((script, []))
            elif mine:
                plan.append((script, [f"--only={','.join(mine)}"]))
        known = scripts | {c for s in SCRIPTS for c in checks_of(s, env)} if checks else scripts
        unknown = sorted(w for w in wanted if w.replace("-", "_") not in known and w not in known)
        if unknown or not plan:
            print(f"stress: no script or check named {unknown or sorted(wanted)}; see --list")
            return 2
    extra = ["--keep"] if args.keep else []
    results = []
    t_all = time.monotonic()
    for script, only in plan:
        print(f"== {script}", flush=True)
        results.append(run_one(script, [*only, *extra], env))
    print("", flush=True)
    print(f"{'script':<18}{'checks':>7}{'ok':>5}{'skip':>6}{'findings':>10}{'secs':>7}  exit", flush=True)
    totals = {"checks": 0, "ok": 0, "skipped": 0, "findings": 0}
    failed = False
    for r in results:
        complete = "checks" in r
        if not complete or r["code"] != 0:
            failed = True
        for k in totals:
            totals[k] += int(r.get(k, 0))  # type: ignore[call-overload]
        cells = [str(r.get(k, "?")) for k in ("checks", "ok", "skipped", "findings")]
        note = "" if complete else "  (no summary line: the script did not finish)"
        print(
            f"{r['script']:<18}{cells[0]:>7}{cells[1]:>5}{cells[2]:>6}{cells[3]:>10}{r['secs']:>7.0f}  {r['code']}{note}",
            flush=True,
        )
    secs = time.monotonic() - t_all
    print(
        f"stress: {len(results)} scripts, {totals['checks']} checks, {totals['ok']} ok, {totals['skipped']} skipped, "
        f"{totals['findings']} findings ({secs:.0f} s)",
        flush=True,
    )
    return 1 if failed or totals["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
