#!/usr/bin/env python3
"""The UI attacker's findings, the daemon's half, as checks of the fixed behaviour (router/PLAN.md Wave 12, T128).

    python scripts/stress/ui.py [--keep] [--only <check>] [--list]

The attacker drove the canvas with Playwright (t3_fail.js, t4_restart.js, t5_reload.js, t6_twotabs.js); those
scripts need node and a browser and are not ported. Their browser half (save retry, reload flush, conflict
banner) is covered by the vitest suite (ui/src/test/saveResilience.test.tsx); this script asserts what the
daemon must do for the UI to get it right:

- lost-update: two tabs that read revision r both save; the second is 409 graph_conflict naming the current
  revision, the first tab's edit is what is stored, and a client that sends no revision still saves.
- append-concurrency: twelve concurrent `POST /projects/{id}/nodes` each get their own node and revision, and all
  twelve are stored.
- graph-saved-revision: the `graph.saved` event a save announces carries the new revision, so another tab can
  tell its copy is stale.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from typing import Any

import _lib
import httpx
from _lib import Daemon, check

_SHARED: Daemon | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="ui-shared").start()
    return _SHARED


def graph_of(d: Daemon, p: str) -> dict[str, Any]:
    g = d.api("GET", f"/projects/{p}/graph", ui=True).json()
    return g.get("graph", g)


def lost_update() -> None:
    d = shared()
    p = "r1"
    d.edit_project(p)
    base = graph_of(d, p)
    rev = base.get("revision")
    if not check(isinstance(rev, int), f"GET graph carries no revision: {sorted(base)}"):
        return
    tab_a = json.loads(json.dumps(base))
    tab_b = json.loads(json.dumps(base))
    for n in tab_a["nodes"]:
        if n["id"] == "edit":
            n["params"]["prompt"] = "TAB A prompt"
    tab_b["nodes"].append({"id": "review-1", "kind": "Colour", "label": "tab B"})
    a = d.api("PUT", f"/projects/{p}/graph", tab_a, ui=True)
    check(a.status_code == 200, f"tab A's save -> {a.status_code} {a.text[:160]}")
    b = d.api("PUT", f"/projects/{p}/graph", tab_b, ui=True)
    body = b.json() if _lib.is_json(b) else {}
    check(
        b.status_code == 409 and body.get("error") == "graph_conflict" and body.get("sent") == rev,
        f"tab B's save on a stale revision -> {b.status_code} {b.text[:160]}",
    )
    stored = graph_of(d, p)
    prompt = next((n.get("params", {}).get("prompt") for n in stored["nodes"] if n["id"] == "edit"), None)
    check(prompt == "TAB A prompt", f"tab A's edit was overwritten: prompt {prompt!r}")
    check(not any(n["id"] == "review-1" for n in stored["nodes"]), "the refused save was stored")
    old_client = {k: v for k, v in stored.items() if k != "revision"}
    r = d.api("PUT", f"/projects/{p}/graph", old_client, ui=True)
    check(r.status_code == 200, f"a save with no revision -> {r.status_code} {r.text[:160]}")


def append_concurrency() -> None:
    d = shared()
    p = "r12"
    d.project(p)
    clients = [d.make_client(ui=True) for _ in range(12)]
    try:
        answers = _lib.concurrently(
            lambda i: clients[i].post(f"/projects/{p}/nodes", json={"kind": "Colour"}), 12
        )
    finally:
        for c in clients:
            c.close()
    codes = Counter(_lib.status_of(r) for r in answers)
    if not check(codes == Counter({201: 12}), f"12 concurrent appends: {dict(codes)}"):
        return
    ids = [r.json()["node_id"] for r in answers]
    revs = sorted(r.json()["revision"] for r in answers)
    check(len(set(ids)) == 12, f"node ids handed out: {sorted(ids)}")
    check(len(set(revs)) == 12, f"revisions handed out: {revs}")
    stored = [n["id"] for n in graph_of(d, p)["nodes"] if n.get("kind") == "Colour"]
    check(sorted(stored) == sorted(ids), f"stored Colour nodes {sorted(stored)} vs answered {sorted(ids)}")


def graph_saved_revision() -> None:
    d = shared()
    p = "r3"
    d.edit_project(p)
    g = graph_of(d, p)
    r = d.api("PUT", f"/projects/{p}/graph", g, ui=True)
    if not check(r.status_code == 200, f"PUT -> {r.status_code}"):
        return
    rev = r.json().get("graph", {}).get("revision")
    if not check(isinstance(rev, int), f"the save answered no revision: {sorted(r.json().get('graph', {}))}"):
        return
    saved = [e for e in bus_events(d, p, 6.0, rev) if _kind(e) == "graph.saved"]
    if not check(saved, "no graph.saved event on GET /events"):
        return
    got = [_revision(e) for e in saved]
    check(rev in got, f"graph.saved events carry revisions {got}, the save answered {rev!r}")


def _kind(e: dict[str, Any]) -> Any:
    return e.get("kind") or (e.get("data") or {}).get("kind")


def _revision(e: dict[str, Any]) -> Any:
    return e.get("revision", (e.get("data") or {}).get("revision"))


def bus_events(d: Daemon, p: str, window_s: float, revision: Any) -> list[dict[str, Any]]:
    """What `GET /events` (the bus, an open SSE stream) replays for the project, until the graph.saved of
    `revision` or `window_s`."""
    out: list[dict[str, Any]] = []
    c = d.make_client(timeout=httpx.Timeout(10.0, read=window_s))
    deadline = time.monotonic() + window_s
    try:
        with c.stream("GET", "/events", params={"project_id": p}) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    try:
                        out.append(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
                    if any(_kind(e) == "graph.saved" and _revision(e) == revision for e in out):
                        break
                if time.monotonic() > deadline:
                    break
    except httpx.ReadTimeout:
        pass
    finally:
        c.close()
    return out


CHECKS = {
    "lost-update": lost_update,
    "append-concurrency": append_concurrency,
    "graph-saved-revision": graph_saved_revision,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
