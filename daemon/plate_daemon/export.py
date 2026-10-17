"""Export a run's result out of the CAS, enforcing doctrine 6 ("gates can block delivery").

DONE exports. GATE_FAIL is kept and flagged and exports only with `override=True` and a written reason of
at least 10 non-blank characters; the override is appended to the run's append-only event log as
`actor="human", reason="override: <reason>"` before the bytes are written. Every other state raises
`NotExportable`. The blob is re-verified against its BLAKE3 key first and written atomically; an existing
destination file is never overwritten.

Two entry points share those rules:

- `export_run(conn, run_id, dest, ...)` copies the stored bytes to one file (the daemon's `POST /runs/{id}/export`).
- `export(run, fmt, out_dir, override=None, ...)` is what `plate export` calls: it decodes the run's gated
  composite (the 16-bit PNG in the CAS), writes it as `tiff16`, `png16` or `exr` with `plate_pixels.imageio`
  (an EXR is the sRGB display master decoded to linear half float, Rec.709 chromaticities), embeds the run's
  lineage in the file (`lineage.embed`: the file name is not the only thing that says where it came from) and
  writes `<name>.gate.json` beside it (the stored gate report plus an `export` block naming the files). The
  sibling is shared by the formats of one take (tiff16 and exr of the same run and name): a second export adds
  its file to the block; a report that belongs to another run blocks the export. The image is written under a
  temp name and moved into place last; a lineage failure leaves nothing behind.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import lineage
from .cas import CAS
from .fsutil import replace_with_retry, temp_sibling, write_bytes_atomic, write_json_atomic
from .machine import append_note, get_run, run_events
from .models import Run
from .states import RunState
from .timeutil import iso, utcnow

MIN_OVERRIDE_REASON = 10
# `plate export --format`: the file extension each format is written with.
FORMATS: dict[str, str] = {"tiff16": ".tif", "png16": ".png", "exr": ".exr"}
OVERRIDE_PREFIX = "override: "
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
REC709_CHROMATICITIES = (0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290)


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


# ---------------------------------------------------------------------------- plate export (still formats)


@dataclass(frozen=True)
class Exported:
    """What `export` wrote: the image, its `.gate.json` sibling and the facts the CLI prints."""

    run_id: str
    fmt: str
    path: Path
    report_path: Path
    overridden: bool
    override_reason: str | None
    lineage_embedded: bool
    report_ref: str | None


def recorded_override(conn: sqlite3.Connection, run_id: str) -> str | None:
    """The reason of the run's latest recorded override (`POST /runs/{id}/override` or an earlier export), else None."""
    reason = None
    for ev in run_events(conn, run_id):
        if ev.kind == "override":
            text = ev.reason or ""
            reason = text[len(OVERRIDE_PREFIX) :] if text.startswith(OVERRIDE_PREFIX) else text
    return reason


def _earlier_report(report_dest: Path, run_id: str) -> dict[str, Any] | None:
    """The `export` block of a `.gate.json` this run already wrote beside another format of the same take (the
    sibling is shared and kept), else None. A report that belongs to anything else blocks the export."""
    if not report_dest.exists():
        return None
    try:
        block = json.loads(report_dest.read_text(encoding="utf-8-sig")).get("export")
    except (OSError, ValueError, AttributeError):
        block = None
    if not isinstance(block, dict) or block.get("run_id") != run_id:
        raise NotExportable(
            f"{report_dest} already exists and is not run {run_id}'s report; exports never overwrite"
        )
    return block


def _decode16(data: bytes, what: str) -> np.ndarray:
    """uint16 RGB (H, W, 3) from image bytes (8-bit sources are promoted by x257)."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise NotExportable(f"{what} is not a decodable image; only image runs export with `plate export`")
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    img = np.ascontiguousarray(img[..., :3][..., ::-1])
    return img.astype(np.uint16) * 257 if img.dtype == np.uint8 else img.astype(np.uint16)


def _write_exr(path: Path, rgb16: np.ndarray) -> None:
    """The sRGB display image as scene-linear half float: the EOTF decode of what the gates scored."""
    from plate_pixels import exr as pexr
    from plate_pixels.ocio import srgb_eotf

    lin = srgb_eotf(rgb16.astype(np.float64) / 65535.0).astype(np.float32)
    pexr.write_multilayer(
        path,
        {"RGBA": {"R": lin[..., 0], "G": lin[..., 1], "B": lin[..., 2]}},
        dtypes={"RGBA": "half"},
        header_attrs={
            "chromaticities": REC709_CHROMATICITIES,
            "plate:colour": "scene-linear Rec.709, the sRGB EOTF applied to the gated display composite",
        },
    )


def _write_image(path: Path, fmt: str, rgb16: np.ndarray) -> None:
    from plate_pixels import imageio

    if fmt == "tiff16":
        imageio.write_tiff16(path, rgb16)
    elif fmt == "png16":
        imageio.write_png16(path, rgb16)
    else:
        _write_exr(path, rgb16)


def _report_for(run: Run, project_dir: Path | None) -> tuple[dict[str, Any], str | None]:
    """(the run's gate report, its `gates/<run>.<n>.json` reference). Stored reports win over the run's summary."""
    if project_dir is not None:
        from .gate_reports import Service

        stored = Service(project_dir).latest(run.id)
        if stored is not None:
            return dict(stored.report), f"gates/{stored.path.name}"
    return dict(run.gate_report or {}), run.gate_report_path


def export(
    run: Run,
    fmt: str,
    out_dir: Path | str,
    override: str | None = None,
    *,
    conn: sqlite3.Connection,
    cas: CAS,
    project_dir: Path | str | None = None,
    name: str | None = None,
) -> Exported:
    """Write the run's result to `out_dir/<name>.<ext>` (+ `<name>.gate.json`); see the module docstring.

    `override` is the written reason (10 characters at least) that lets a GATE_FAIL run out; a reason recorded
    earlier on the run counts when none is given. `name` defaults to the run id. Raises `NotExportable` for a
    run that may not leave (GATE_FAIL without a reason, any state but DONE and GATE_FAIL, no result blob, a
    blob that fails its hash, a destination that exists) and `ValueError` for an unknown format or name."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    stem = name or run.id
    if not NAME_RE.match(stem):
        raise ValueError(
            f"export name {stem!r}: letters, digits, '.', '_' and '-' only (it is a Windows file name)"
        )
    out_dir = Path(out_dir)
    project_dir = Path(project_dir) if project_dir is not None else None
    reason = override.strip() if override is not None else None
    reason = reason or None
    if run.state is RunState.GATE_FAIL:
        reason = reason or recorded_override(conn, run.id)
        if reason is None:
            raise NotExportable(
                f"run {run.id} failed its gates; export needs a written override reason of at least "
                f"{MIN_OVERRIDE_REASON} characters"
            )
        if len(reason) < MIN_OVERRIDE_REASON:
            raise NotExportable(
                f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters"
            )
    elif run.state is not RunState.DONE:
        raise NotExportable(
            f"run {run.id} is {run.state.value}; only DONE (or GATE_FAIL with override) exports"
        )
    key = run.outputs[0] if run.outputs else run.result_key
    if not key:
        raise NotExportable(f"run {run.id} has no result blob")
    if not cas.verify(key):
        raise NotExportable(f"result blob {key} is missing or fails its BLAKE3 check")
    dest = out_dir / f"{stem}{FORMATS[fmt]}"
    report_dest = out_dir / f"{stem}.gate.json"
    if dest.exists():
        raise NotExportable(f"{dest} already exists; exports never overwrite")
    earlier = _earlier_report(report_dest, run.id)
    rgb16 = _decode16(cas.read_bytes(key), f"result blob {key[:12]}")
    report, report_ref = _report_for(run, project_dir)
    lin = lineage.Lineage(
        project_id=run.project_id,
        node_id=run.node_id or "none",
        run_id=run.id,
        packet_id=run.packet_id,
        engine_id=run.route.engine_id,
        engine_version=str(run.route_dict.get("model_version") or "") or None,
        gate_report=report_ref,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # the temp name keeps the real extension: the writers and `lineage.embed` pick their codec by it
    image_tmp = temp_sibling(dest).with_suffix(dest.suffix)
    try:
        _write_image(image_tmp, fmt, rgb16)
        try:
            lineage.embed(image_tmp, lin)
        except (lineage.LineageError, OSError) as e:
            raise NotExportable(
                f"could not embed lineage in the {fmt} file ({e}); nothing was exported"
            ) from e
        doc = dict(report)
        doc["export"] = {
            "run_id": run.id,
            "files": [*((earlier or {}).get("files") or []), dest.name],
            "state": run.state.value,
            "override_reason": reason if run.state is RunState.GATE_FAIL else None,
            "gate_report": report_ref,
            "exported_utc": (earlier or {}).get("exported_utc") or iso(utcnow()),
        }
        if run.state is RunState.GATE_FAIL and recorded_override(conn, run.id) is None:
            append_note(conn, run.id, kind="override", actor="human", reason=f"{OVERRIDE_PREFIX}{reason}")
        write_json_atomic(report_dest, doc, indent=1)
        try:
            replace_with_retry(image_tmp, dest)
        except OSError:
            if earlier is None:  # a shared sibling written for another format stays
                report_dest.unlink(missing_ok=True)
            raise
    finally:
        image_tmp.unlink(missing_ok=True)
    return Exported(
        run_id=run.id,
        fmt=fmt,
        path=dest,
        report_path=report_dest,
        overridden=run.state is RunState.GATE_FAIL,
        override_reason=reason if run.state is RunState.GATE_FAIL else None,
        lineage_embedded=True,
        report_ref=report_ref,
    )
