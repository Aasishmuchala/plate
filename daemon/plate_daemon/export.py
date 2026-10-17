"""Export a run's result out of the CAS, enforcing doctrine 6 ("gates can block delivery").

DONE exports. GATE_FAIL is kept and flagged and exports only with `override=True` and a written reason of
at least 10 non-blank characters; the override is appended to the run's append-only event log as
`actor="human", reason="override: <reason>"` before the bytes are written. Every other state raises
`NotExportable`. The blob is re-verified against its BLAKE3 key first and written atomically; an existing
destination file is never overwritten.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .cas import CAS
from .fsutil import write_bytes_atomic
from .machine import append_note, get_run
from .states import RunState

MIN_OVERRIDE_REASON = 10


class NotExportable(Exception):
    pass


def export_run(
    conn: sqlite3.Connection,
    run_id: str,
    dest: Path,
    *,
    override: bool = False,
    reason: str | None = None,
    cas: CAS | None = None,
) -> Path:
    if cas is None:  # no global CAS (router plan T5): the caller passes the run's project CAS
        raise TypeError("pass the project's CAS (CAS(project_dir / 'assets')); there is no global CAS")
    dest = Path(dest)
    run = get_run(conn, run_id)
    if run.state is RunState.GATE_FAIL:
        if not override:
            raise NotExportable(
                f"run {run_id} failed its gates; export needs override=True and a written reason"
            )
        if reason is None or len(reason.strip()) < MIN_OVERRIDE_REASON:
            raise NotExportable(
                f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters"
            )
    elif run.state is not RunState.DONE:
        raise NotExportable(
            f"run {run_id} is {run.state.value}; only DONE (or GATE_FAIL with override) exports"
        )
    if not run.result_key:
        raise NotExportable(f"run {run_id} has no result blob")
    if not cas.verify(run.result_key):
        raise NotExportable(f"result blob {run.result_key} is missing or fails its BLAKE3 check")
    if dest.exists():
        raise NotExportable(f"{dest} already exists; exports never overwrite")
    if run.state is RunState.GATE_FAIL:
        append_note(conn, run_id, kind="override", actor="human", reason=f"override: {reason}")
    write_bytes_atomic(dest, cas.read_bytes(run.result_key))
    return dest
