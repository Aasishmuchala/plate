"""Label records and the labels.jsonl store: the record schema, a Windows-safe one-line append,
a tolerant reader, the per-rater "already labelled" key set and the atomic line removal behind undo.
New code for PLATE. Files are UTF-8 with LF newlines on both OSes.

Where the file lives: labels are calibration data made from client work, so by default they are in the app-data
folder, never in the repo: `<data home>/calib/labels.jsonl`, where the data home is `PLATE_HOME`, else
`platformdirs.user_data_dir("Plate", appauthor=False)` (the daemon's `config.app_home`, mirrored here so the
labeller stays free of daemon imports). `PLATE_LABELS` names the exact file instead. The daemon's verdict route
resolves the same way (`plate_daemon.verdicts.labels_path`), so a canvas verdict and a three-key label land in
one file. A repo-local `calib/labels.jsonl` left by an older build is read once (see `legacy_labels`) with a
printed warning and is never written.

One line per verdict:
    {"v":1,"at":"2026-10-17T09:41:07Z","rater":"aasish","take_id":"S3","plate":"...","candidate":"...",
     "run_id":"r12","verdict":"fix","reasons":["P1","A2"],"seconds":14.2,"gate_report":"gates/r12.1.json"}
`run_id` and `gate_report` are left out when unknown; `plate` is null for a candidate-only label.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import platformdirs

from plate_label.codes import validate_reasons
from plate_label.pairs import Pair

SCHEMA_VERSION = 1
MAX_SECONDS = 86_400.0

HOME_ENV = "PLATE_HOME"
LABELS_ENV = "PLATE_LABELS"
LABELS_NAME = "labels.jsonl"
# tools/plate_label/store.py -> parents[2] is the repo root. A calib/labels.jsonl there was written by an older
# build: it is only ever read (legacy_labels), never written.
LEGACY_OUT = Path(__file__).resolve().parents[2] / "calib" / LABELS_NAME


def data_home(env: Mapping[str, str] | None = None) -> Path:
    """`PLATE_HOME`, else the OS app-data folder (`%LOCALAPPDATA%\\Plate` on Windows, Application Support on Mac)."""
    env = os.environ if env is None else env
    if env.get(HOME_ENV):
        return Path(env[HOME_ENV])
    return Path(platformdirs.user_data_dir("Plate", appauthor=False))


def default_labels_path(env: Mapping[str, str] | None = None) -> Path:
    """`PLATE_LABELS`, else `<data home>/calib/labels.jsonl`: the file the daemon's verdict route writes too."""
    env = os.environ if env is None else env
    if env.get(LABELS_ENV):
        return Path(env[LABELS_ENV])
    return data_home(env) / "calib" / LABELS_NAME


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


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _read_file(path: Path) -> list[dict]:
    """The records of one labels file in file order; an unreadable line warns and is skipped."""
    out: list[dict] = []
    with open(path, encoding="utf-8", newline="") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                warnings.warn(f"{path}:{n}: skipped an unreadable label line", stacklevel=3)
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


_LEGACY_LOCK = threading.Lock()
# legacy file (normalised path) -> its records, filled by the first read in this process.
_LEGACY_SEEN: dict[str, list[dict]] = {}


def legacy_labels(target: Path) -> list[dict]:
    """Records of a repo-local `calib/labels.jsonl` left by an older build, else `[]`.

    Read once per process (the records are cached) with one printed warning that says where labels live now.
    The file is never written, truncated or removed. `[]` when it does not exist or when it is `target` itself
    (the data folder or `PLATE_LABELS` was pointed at the repo on purpose)."""
    legacy = Path(LEGACY_OUT)
    target = Path(target)
    if not legacy.is_file() or _same_file(legacy, target):
        return []
    key = os.path.normcase(os.path.abspath(legacy))
    with _LEGACY_LOCK:
        if key in _LEGACY_SEEN:
            return list(_LEGACY_SEEN[key])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a torn tail is not worth a second warning
            records = _read_file(legacy)
        _LEGACY_SEEN[key] = records
    print(
        f"warning: found {legacy} left by an older build ({len(records)} label(s)). It is read, never "
        f"written: labels now go to {target}. Move it there to keep them, or delete it.",
        file=sys.stderr,
        flush=True,
    )
    return list(records)


def read_labels(path: Path, legacy: bool | None = None) -> list[dict]:
    """All records in file order. A missing file is empty; an unreadable line warns and is skipped.

    Reading the default labels file (`default_labels_path()`) also returns the records of a repo-local legacy
    file first (`legacy_labels`); `legacy=True` or `False` forces that either way."""
    path = Path(path)
    out: list[dict] = []
    if legacy is None:
        legacy = _same_file(path, default_labels_path())
    if legacy:
        out.extend(legacy_labels(path))
    if path.exists():
        out.extend(_read_file(path))
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
