"""Label records and the calib/labels.jsonl store: the record schema, a Windows-safe one-line append,
a tolerant reader, the per-rater "already labelled" key set and the atomic line removal behind undo.
New code for PLATE. Files are UTF-8 with LF newlines on both OSes.

One line per verdict:
    {"v":1,"at":"2026-10-17T09:41:07Z","rater":"aasish","take_id":"S3","plate":"...","candidate":"...",
     "run_id":"r12","verdict":"fix","reasons":["P1","A2"],"seconds":14.2,"gate_report":"gates/r12.1.json"}
`run_id` and `gate_report` are left out when unknown; `plate` is null for a candidate-only label.
"""

from __future__ import annotations

import json
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from plate_label.codes import validate_reasons
from plate_label.pairs import Pair

SCHEMA_VERSION = 1
MAX_SECONDS = 86_400.0

# <repo>/calib/labels.jsonl (tools/plate_label/store.py -> parents[2] is the repo root). Gitignored.
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "calib" / "labels.jsonl"


def utc_now_iso() -> str:
    """ISO-8601 UTC with a trailing Z. Colons are fine inside file content, never in file names."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_seconds(value: object) -> float:
    """A duration from the client: finite, 0 .. 24 h, rounded to 0.1 s. ValueError otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a number")
    secs = float(value)
    if not (0.0 <= secs <= MAX_SECONDS):  # also false for NaN
        raise ValueError(f"seconds must be between 0 and {int(MAX_SECONDS)}")
    return round(secs, 1)


@dataclass
class LabelRecord:
    at: str
    rater: str
    take_id: str
    plate: str | None
    candidate: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    seconds: float = 0.0
    run_id: str | None = None
    gate_report: str | None = None
    v: int = SCHEMA_VERSION

    @classmethod
    def for_pair(cls, pair: Pair, verdict: str, reasons: object, seconds: object, rater: str) -> LabelRecord:
        """Validate and build a record for `pair`; ValueError for a bad verdict / reasons / seconds."""
        if not isinstance(rater, str) or not rater.strip():
            raise ValueError("rater must be a non-empty string")
        return cls(
            at=utc_now_iso(),
            rater=rater.strip(),
            take_id=pair.take_id,
            plate=pair.plate_ref,
            candidate=pair.candidate_ref,
            verdict=verdict,
            reasons=validate_reasons([] if reasons is None else reasons, verdict),
            seconds=clean_seconds(seconds),
            run_id=pair.run_id,
            gate_report=pair.gate_report,
        )

    def to_dict(self) -> dict:
        """Schema key order; the optional keys are absent (not null) when unknown."""
        out: dict = {
            "v": self.v,
            "at": self.at,
            "rater": self.rater,
            "take_id": self.take_id,
            "plate": self.plate,
            "candidate": self.candidate,
        }
        if self.run_id is not None:
            out["run_id"] = self.run_id
        out["verdict"] = self.verdict
        out["reasons"] = list(self.reasons)
        out["seconds"] = self.seconds
        if self.gate_report is not None:
            out["gate_report"] = self.gate_report
        return out

    def to_json(self) -> str:
        """One line, no trailing newline."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @property
    def key(self) -> tuple[str, str | None, str]:
        return (self.take_id, self.plate, self.candidate)


def _ends_without_newline(path: Path) -> bool:
    """True when the file exists, is non-empty and its last byte is not LF (a crash mid-write)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except FileNotFoundError:
        return False


def append_label(path: Path, rec: LabelRecord) -> str:
    """Append one record as one line (a single write call so concurrent appenders cannot interleave).
    Returns the JSON line that was written, for undo."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if _ends_without_newline(path) else ""
    line = rec.to_json()
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(lead + line + "\n")
        f.flush()
    return line


def read_labels(path: Path) -> list[dict]:
    """All records in file order. A missing file is empty; an unreadable line warns and is skipped."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8", newline="") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                warnings.warn(f"{path}:{n}: skipped an unreadable label line", stacklevel=2)
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def labelled_keys(path: Path, rater: str) -> set[tuple[str, str | None, str]]:
    """(take_id, plate, candidate) of every pair `rater` has already labelled."""
    return {
        (r.get("take_id"), r.get("plate"), r.get("candidate"))
        for r in read_labels(path)
        if r.get("rater") == rater and r.get("take_id") and r.get("candidate")
    }


def replace_with_retry(src: Path, dst: Path, tries: int = 6) -> None:
    """os.replace, retried with backoff: on Windows, Defender or an indexer may hold either file briefly."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05 * 2**attempt)


def remove_line(path: Path, line: str) -> bool:
    """Remove the LAST line equal to `line` from the labels file (atomic rewrite). False when absent.

    Undo only ever removes a line this process wrote, so the file otherwise stays append-only."""
    path = Path(path)
    if not path.exists():
        return False
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.read().split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == line:
            del lines[i]
            break
    else:
        return False
    text = "\n".join(lines)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    replace_with_retry(tmp, path)
    return True
