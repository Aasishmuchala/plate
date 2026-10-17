"""Carry-over on supersede: the callers of `revisions.carry_over` (router plan T96, decision 25).

A client revision re-renders a camera: the new packet `supersedes` the old one and `revisions.on_new_packet` marks
the old packet's runs and re-binds the Plate nodes. The layers the artist already accepted should not be lost, so
this module gathers them and hands them to `revisions.carry_over`:

    approved_layers(conn, project, packet_id)                 -> [{run_id, mask_hash, ids, rgb16_hash}]
    on_supersede(pipeline, project, new_pv, old_packet_id)    -> [result]      carry, ledger, publish
    auto_enabled(project)                                     -> bool

An approved layer is a DONE run of the packet that carries an `accept` note (the note `POST /runs/{id}/accept` writes
after a logged full scrub), so a take nobody accepted never rides along. Its fields come from the run as the pipeline
stored it: `mask_hash` is `normalized.mask_union_key` (the union of a chain's masks, T92) else `normalized.mask_key`,
`ids` is `inputs.mask.ids`, `rgb16_hash` is `outputs[0]` (the composite). A layer whose record lacks one of them is
still listed, with the field `None` or `[]`: `carry_over` drops it as `layer_invalid` and the artist reads why
(a painted-only mask has no legend ids to compare, so nothing can show it is unchanged).

`on_supersede` is the whole reaction: it reads the old and the new plate through the pipeline's packet cache, lets
`revisions.carry_over` decide and persist (each carried layer is a new `done` ledger record first, then an index row,
`engine_id: carry-over`, no spend), and publishes one `packet.carried` record
`{kind, packet_id, supersedes, carried, dropped, results}` for the UI to refresh from. Every carried layer is a new
take the artist still has to accept; nothing is accepted for them. The caller does NOT need to hold the project
lock: the ledger takes it for each append. A caller that does hold it passes `lock=` (the `projects.Lock`), or the
ledger would wait on its own process and time out. The same layer carried twice answers the first run, so a button
pressed twice (or an ingest replayed) writes nothing new.

`auto_enabled(project)` is the switch T107 reads before calling this from the packet-ingest hook:
`project.json` `settings.auto_carry_over` (default true), and `PLATE_AUTO_CARRY_OVER=0` turns it off for the whole
daemon. The route `POST /packets/{id}/carry-over` (routes/carryover.py) is the manual button and ignores the switch.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import revisions
from .ledger import Ledger
from .machine import get_run
from .projects import Project
from .states import RunState

__all__ = ["ENV_SWITCH", "approved_layers", "auto_enabled", "on_supersede", "summarise"]

log = logging.getLogger(__name__)

ENV_SWITCH = "PLATE_AUTO_CARRY_OVER"
_OFF = {"0", "false", "no", "off"}
ACCEPT = "accept"

# One carry-over at a time per project: two clicks (or a click during the ingest hook) must not race the
# "already carried" check and write the same layer twice.
_GUARDS: dict[str, threading.Lock] = {}
_GUARDS_LOCK = threading.Lock()


def _guard(project_id: str) -> threading.Lock:
    with _GUARDS_LOCK:
        return _GUARDS.setdefault(project_id, threading.Lock())


def auto_enabled(project: Project, env: Mapping[str, str] | None = None) -> bool:
    """Does a superseding packet carry the approved layers over by itself? `PLATE_AUTO_CARRY_OVER=0` says no for
    every project; otherwise the project's `settings.auto_carry_over` (absent: yes)."""
    raw = (os.environ if env is None else env).get(ENV_SWITCH)
    if raw is not None and raw.strip().lower() in _OFF:
        return False
    settings = project.doc.get("settings") if isinstance(project.doc, Mapping) else None
    value = settings.get("auto_carry_over", True) if isinstance(settings, Mapping) else True
    if isinstance(value, str):
        return value.strip().lower() not in _OFF
    return bool(value) if value is not None else True


def _hash(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def approved_layers(
    conn: sqlite3.Connection, project: Project | None, packet_id: str
) -> list[dict[str, Any]]:
    """`[{run_id, mask_hash, ids, rgb16_hash}]` for the accepted DONE runs of `packet_id`, oldest first (see the
    module docstring for where each field comes from). `project` guards a shared index: a run that names another
    project is left out."""
    rows = conn.execute(
        """SELECT r.id FROM runs r WHERE r.packet_id = ? AND r.state = ?
           AND EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = r.id AND e.kind = ?)
           ORDER BY r.created_utc, r.id""",
        (packet_id, RunState.DONE.value, ACCEPT),
    ).fetchall()
    layers: list[dict[str, Any]] = []
    for row in rows:
        run = get_run(conn, row["id"])
        if project is not None and run.project_id != project.project_id:
            continue
        norm = run.normalized or {}
        mask = run.inputs.get("mask") if isinstance(run.inputs, Mapping) else None
        ids = mask.get("ids") if isinstance(mask, Mapping) else None
        outputs = run.outputs
        layers.append(
            {
                "run_id": run.id,
                "mask_hash": _hash(norm.get("mask_union_key")) or _hash(norm.get("mask_key")),
                "ids": [str(i) for i in ids] if isinstance(ids, (list, tuple)) else [],
                "rgb16_hash": _hash(outputs[0]) if outputs else None,
            }
        )
    return layers


def _plain(value: Any) -> Any:
    """JSON-safe: numpy scalars as numbers, NaN and infinities as None, paths as text."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    item = getattr(value, "item", None)  # numpy scalar
    if callable(item):
        try:
            return _plain(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def summarise(results: list[dict[str, Any]]) -> tuple[int, int]:
    """(carried, dropped) of a `carry_over` answer."""
    carried = sum(1 for r in results if r.get("status") == "carried")
    return carried, len(results) - carried


def on_supersede(
    pipeline: Any,
    project: Project,
    new_pv: Any,
    old_packet_id: str,
    *,
    bus: Any = None,
    now: datetime | None = None,
    lock: Any = None,
) -> list[dict[str, Any]]:
    """Carry the approved layers of `old_packet_id` onto `new_pv` (an ingested packet of the same project) and
    publish `packet.carried`. Returns `carry_over`'s results, one per layer, JSON-safe. Raises `PipelineError` when
    the old packet is unknown or not ingested, `revisions.RevisionError` when the new one is not in the index."""
    pid = project.project_id
    bus = bus if bus is not None else getattr(pipeline, "bus", None)
    old_pv = pipeline.packet(pid, old_packet_id)
    clock = (lambda: now) if now is not None else getattr(pipeline, "clock", None)
    with _guard(pid), pipeline.index.project(pid) as conn:
        layers = approved_layers(conn, project, old_packet_id)
        results: list[dict[str, Any]] = []
        if layers:
            ledger = Ledger(project.root, lock=lock, **({"clock": clock} if callable(clock) else {}))
            results = revisions.carry_over(
                old_pv,
                new_pv,
                layers,
                project=project,
                conn=conn,
                cas=pipeline.cas_for(pid),
                ledger=ledger,
                bus=bus,
                now=now,
            )
    results = [_plain(r) for r in results]
    carried, dropped = summarise(results)
    event = {
        "kind": "packet.carried",
        "packet_id": new_pv.packet_id,
        "supersedes": old_packet_id,
        "carried": carried,
        "dropped": dropped,
        "results": results,
    }
    if bus is not None:
        try:
            bus.publish(None, event, project_id=pid)
        except Exception as e:  # the bus is for watchers; a failed publish never undoes a carried layer
            log.warning("packet.carried for %s not published: %s", new_pv.packet_id, e)
    return results
