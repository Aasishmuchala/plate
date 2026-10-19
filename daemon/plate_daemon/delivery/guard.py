"""What a delivered file must carry, and who may deliver it (doctrine 6: gates can block delivery).

`Provenance` is the delivery-side view of a run: the lineage record that goes inside the file (T26), the
gate report that scored it, the run's state, whether it is previs, and an optional override. `authorize`
decides before a single byte is produced:

    previs                        refused outright, no override (canvas rule PREVIS_NOT_DELIVERABLE)
    state other than DONE or      refused (RUN_NOT_DELIVERABLE)
    GATE_FAIL
    no gate report                refused (NO_GATE_REPORT) unless the caller says the output was never scored
    GATE_FAIL, or a report whose  refused (GATE_FAIL) without an override reason of at least 10 non-blank
    verdict is `fail`             characters; with one, `on_override(reason)` runs first (so the run's event
                                  log hears about it before the bytes exist) and the sidecar records it

Every delivered file then gets its lineage embedded (`plate_daemon.lineage.embed`) and a sidecar
`<file name>.gate.json` (a sequence gets one for the whole sequence) holding the lineage, the full versioned
gate report, the verdict, any override, the colour tags and the BLAKE3 of each file. Sidecars carry names,
never absolute paths. A file is staged under a hidden sibling name, finished, then moved into place with
`os.replace` (retried on Windows); an existing destination is never overwritten. The destination is claimed,
not checked (`export.claim`, `O_CREAT | O_EXCL`): of any number of deliveries racing to one name exactly one
lands and the others raise `FileExistsError` (a route's 409 `exists`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blake3 import blake3

from .. import lineage as lineage_mod
from ..export import claim
from ..fsutil import read_json, replace_with_retry, write_json_atomic
from ..lineage import Lineage
from ..timeutil import iso, utcnow

__all__ = [
    "MIN_OVERRIDE_REASON",
    "SIDECAR_SUFFIX",
    "DeliveryError",
    "DeliveryRefused",
    "Provenance",
    "ToolMissing",
    "authorize",
    "blake3_file",
    "embed_and_place",
    "read_sidecar",
    "sequence_stem",
    "sidecar_path",
    "stage_path",
    "write_sidecar",
]

MIN_OVERRIDE_REASON = 10
SIDECAR_SUFFIX = ".gate.json"
SIDECAR_SCHEMA = 1
DELIVERABLE_STATES = ("DONE", "GATE_FAIL")
_FRAME_FIELD = re.compile(r"%0?\d*d")
_HASH_CHUNK = 1 << 20

PREVIS_NOT_DELIVERABLE = "PREVIS_NOT_DELIVERABLE"


class DeliveryError(RuntimeError):
    pass


class ToolMissing(DeliveryError):
    """ffmpeg or ffprobe is not installed (tests skip on this)."""


class DeliveryRefused(DeliveryError):
    """Doctrine said no. `code` is stable: PREVIS_NOT_DELIVERABLE, RUN_NOT_DELIVERABLE, NO_GATE_REPORT,
    GATE_FAIL, OVERRIDE_REASON_TOO_SHORT, NO_CLIPS."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _plain_report(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    if hasattr(report, "model_dump"):
        return dict(report.model_dump(mode="json"))
    if isinstance(report, Mapping):
        return dict(report)
    raise DeliveryError(f"a gate report is a dict or a GateReport, not {type(report).__name__}")


@dataclass(frozen=True)
class Provenance:
    """`gate_report`: a `plate_gates.report.GateReport`, a stored report's dict, or None (never scored).
    `report_ref` is where the report lives in the project (`gates/<run_id>.<n>.json`). `run_state` is a
    RunState or its string. `on_override` is called with the reason before an overridden delivery starts."""

    lineage: Lineage
    gate_report: Any = None
    run_state: str = "DONE"
    previs: bool = False
    override_reason: str | None = None
    report_ref: str | None = None
    on_override: Callable[[str], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_report", _plain_report(self.gate_report))
        object.__setattr__(self, "run_state", str(self.run_state))

    @property
    def verdict(self) -> str:
        report = self.gate_report
        return str(report.get("verdict")) if report and report.get("verdict") else "not_scored"

    @property
    def failed(self) -> bool:
        return self.run_state == "GATE_FAIL" or self.verdict == "fail"

    @classmethod
    def from_run(
        cls,
        run: Any,
        *,
        project_dir: str | Path | None = None,
        gate_report: Any = None,
        override_reason: str | None = None,
        conn: Any = None,
        engine_version: str | None = None,
    ) -> Provenance:
        """Build from a `models.Run`. With `project_dir` and no explicit report, the newest stored report
        (`gates/<run_id>.<n>.json`) is used and referenced. With `conn`, an override is appended to the
        run's event log exactly as `export.export` does."""
        report, ref = gate_report, None
        if report is None and project_dir is not None:
            from ..gate_reports import Service

            stored = Service(project_dir).latest(run.id)
            if stored is not None:
                report, ref = stored.report, f"gates/{run.id}.{stored.n}.json"
        hook = _override_logger(conn, run.id) if conn is not None else None
        flagged = any(bool(d.get("previs")) for d in (run.params, run.inputs, run.normalized or {}))
        return cls(
            lineage=Lineage(
                project_id=run.project_id,
                node_id=run.node_id or "unassigned",
                run_id=run.id,
                packet_id=run.packet_id,
                engine_id=run.route.engine_id,
                engine_version=engine_version,
                gate_report=ref,
            ),
            gate_report=report,
            run_state=str(run.state),
            previs=flagged,
            override_reason=override_reason,
            report_ref=ref,
            on_override=hook,
        )


def _override_logger(conn: Any, run_id: str) -> Callable[[str], None]:
    from ..machine import append_note

    def log(reason: str) -> None:
        append_note(conn, run_id, kind="override", actor="human", reason=f"override: {reason}")

    return log


def authorize(prov: Provenance, *, require_report: bool = True) -> dict[str, str] | None:
    """Raise DeliveryRefused, or return the override record to store in the sidecar (None when no override
    was needed). Runs before any output is produced."""
    if prov.previs:
        raise DeliveryRefused(
            PREVIS_NOT_DELIVERABLE,
            "previs output is a draft; it is never delivered (re-run it as a real take or render it)",
        )
    if prov.run_state not in DELIVERABLE_STATES:
        raise DeliveryRefused(
            "RUN_NOT_DELIVERABLE",
            f"run {prov.lineage.run_id} is {prov.run_state}; only DONE (or GATE_FAIL with an override) delivers",
        )
    if require_report and prov.gate_report is None:
        raise DeliveryRefused(
            "NO_GATE_REPORT", "every delivered output carries a gate report; this run has none"
        )
    if not prov.failed:
        return None
    reason = (prov.override_reason or "").strip()
    if not prov.override_reason:
        raise DeliveryRefused(
            "GATE_FAIL",
            f"run {prov.lineage.run_id} failed its gates; delivery needs an override and a written reason",
        )
    if len(reason) < MIN_OVERRIDE_REASON:
        raise DeliveryRefused(
            "OVERRIDE_REASON_TOO_SHORT",
            f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters",
        )
    if prov.on_override is not None:
        prov.on_override(reason)
    return {"reason": reason, "at_utc": iso(utcnow())}


# ---------------------------------------------------------------------------- files and sidecars


def blake3_file(path: Path) -> str:
    h = blake3()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def stage_path(final: Path) -> Path:
    """A hidden sibling that keeps the real suffix (lineage and ffmpeg pick the carrier from it)."""
    return final.with_name(f".{uuid.uuid4().hex[:12]}.{final.name}")


def embed_and_place(staged: Path, final: Path, lineage: Lineage) -> None:
    """Write `lineage` into the staged file, claim `final` and move the staged file over the empty claim.
    Never overwrites: a name that is taken raises FileExistsError, and a failure after the claim gives the
    name back."""
    lineage_mod.embed(staged, lineage)
    if not claim(final):
        raise FileExistsError(f"{final} already exists; delivery never overwrites")
    try:
        replace_with_retry(staged, final)
    except BaseException:
        final.unlink(missing_ok=True)
        raise


def sequence_stem(pattern_name: str) -> str | None:
    """`shot.%04d.exr` -> `shot`; None when the name has no frame field."""
    m = _FRAME_FIELD.search(pattern_name)
    if m is None:
        return None
    return pattern_name[: m.start()].rstrip("._-") or "sequence"


def sidecar_path(out: str | Path) -> Path:
    """`shot.mov` -> `shot.mov.gate.json`; `shot.%04d.exr` -> `shot.gate.json`."""
    out = Path(out)
    stem = sequence_stem(out.name)
    return out.with_name((stem + SIDECAR_SUFFIX) if stem is not None else out.name + SIDECAR_SUFFIX)


def write_sidecar(
    out: Path,
    prov: Provenance,
    *,
    fmt: str,
    files: Iterable[Path],
    override: dict[str, str] | None,
    color: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Write the `.gate.json` for `out` (see the module docstring) and return its path."""
    listed = [{"name": f.name, "bytes": f.stat().st_size, "blake3": blake3_file(f)} for f in sorted(files)]
    body: dict[str, Any] = {
        "schema": SIDECAR_SCHEMA,
        "kind": "plate.delivery",
        "format": fmt,
        "created_utc": iso(utcnow()),
        "files": listed,
        "lineage": prov.lineage.to_dict(),
        "run_state": prov.run_state,
        "previs": False,
        "gate": {"verdict": prov.verdict, "report_ref": prov.report_ref, "report": prov.gate_report},
        "override": override,
        "color": dict(color) if color else None,
    }
    if extra:
        body.update(dict(extra))
    path = sidecar_path(out)
    write_json_atomic(path, body, indent=1)
    return path


def read_sidecar(path: str | Path) -> dict[str, Any] | None:
    """The sidecar next to a delivered file, or None when there is none or it is unreadable."""
    p = sidecar_path(path)
    if not p.is_file():
        return None
    try:
        data = read_json(p)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
