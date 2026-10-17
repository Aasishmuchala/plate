"""Human verdicts: Ship / Fix / Render-it on a take, as calibration labels and as ledger evidence.

`POST /runs/{id}/verdict` (routes/misc.py) lands here. A verdict is only taken against a logged FULL scrub of
the same run (the same rule `POST /runs/{id}/accept` applies), carries reason codes from
`calib/reason_codes.yaml` (Fix and Render-it only; a Ship carries none) and goes two places:

- `<data home>/calib/labels.jsonl`, one line per verdict in the labelling tool's schema
  (`tools/plate_label/store.py`, v1: `at`, `rater`, `take_id`, `plate`, `candidate`, `run_id`, `verdict`,
  `reasons`, `gate_report`), so `plate_gates.calibration` reads daemon verdicts and labeller verdicts alike.
  A relabel of one take by one rater is a later line: the calibration report keeps the last. Labels are
  calibration data made from client work, so by default they live in the app-data folder (`PLATE_HOME`,
  else `platformdirs`: `%LOCALAPPDATA%\\Plate` on Windows, `~/Library/Application Support/Plate` on macOS),
  never inside the repo.
- the project's hash-chained `runs.jsonl` as kind `verdict`, with `data.decision` set to `accepted` for a
  Ship and `rerolled` for a Fix or Render-it, which is what `ledger.summarize_records` counts per engine.

`labels_path(settings)` is `settings.labels_path` when the settings carry one, else `PLATE_LABELS`, else
`<settings.home>/calib/labels.jsonl` (`config.app_home()` when there are no settings): the file the labeller
writes by default too, so a canvas verdict and a three-key label land in one file.

A repo-local `calib/labels.jsonl` left by an older build is a legacy file: `legacy_labels` reads it once per
process, prints one warning naming where labels go now, and nothing ever writes to it. Appends are one `write`
per line under a thread lock, and a line that follows a torn tail starts on a fresh line, like the labeller's
own appender.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import app_home

__all__ = [
    "DECISION",
    "LABEL_VERSION",
    "VERDICTS",
    "VerdictError",
    "append_label",
    "build_label",
    "check_reasons",
    "default_labels_path",
    "labels_path",
    "legacy_labels",
    "reason_codes",
]

VERDICTS: tuple[str, ...] = ("ship", "fix", "render_it")
NEEDS_REASONS = frozenset({"fix", "render_it"})
# What ledger.summarize_records counts: a Ship is an accepted take, a Fix or Render-it sends it back.
DECISION = {"ship": "accepted", "fix": "rerolled", "render_it": "rerolled"}
LABEL_VERSION = 1
MAX_SECONDS = 86_400.0

# daemon/plate_daemon/verdicts.py -> parents[2] is the repo root: home of the reason-code table, and of the
# labels file an older build wrote there (read-only now).
REPO = Path(__file__).resolve().parents[2]
LABELS_NAME = "labels.jsonl"
LEGACY_LABELS = REPO / "calib" / LABELS_NAME
DEFAULT_CODES = REPO / "calib" / "reason_codes.yaml"
LABELS_ENV = "PLATE_LABELS"
CODES_ENV = "PLATE_REASON_CODES"

_APPEND_LOCK = threading.Lock()
_LEGACY_LOCK = threading.Lock()
# legacy file (normalised path) -> its records, filled by the first read in this process.
_LEGACY_SEEN: dict[str, list[dict[str, Any]]] = {}


class VerdictError(ValueError):
    """A verdict the daemon refuses (bad verdict name, unknown reason code, unusable labels file)."""


def default_labels_path(settings: Any = None) -> Path:
    """`<settings.home>/calib/labels.jsonl`, or under `config.app_home()` (PLATE_HOME, else the OS data dir)."""
    home = getattr(settings, "home", None)
    return (Path(home) if home else app_home()) / "calib" / LABELS_NAME


def labels_path(settings: Any = None) -> Path:
    """Where verdict labels go: `settings.labels_path`, else `PLATE_LABELS`, else the data-folder default."""
    declared = getattr(settings, "labels_path", None)
    if declared:
        return Path(declared)
    env = os.environ.get(LABELS_ENV)
    if env:
        return Path(env)
    target = default_labels_path(settings)
    legacy_labels(target)  # an old repo-local file is noticed (one warning), never lost and never written
    return target


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def legacy_labels(target: Path | str) -> list[dict[str, Any]]:
    """Records of a repo-local `calib/labels.jsonl` left by an older build, else `[]`.

    The file is read once per process (the records are cached) with one printed warning that says where labels
    live now. It is never written, truncated or removed. `[]` when it does not exist or when it is `target`
    itself (someone pointed the data folder, or `PLATE_LABELS`, at the repo on purpose).
    """
    legacy = Path(LEGACY_LABELS)
    target = Path(target)
    if not legacy.is_file() or _same_file(legacy, target):
        return []
    key = os.path.normcase(os.path.abspath(legacy))
    with _LEGACY_LOCK:
        if key in _LEGACY_SEEN:
            return list(_LEGACY_SEEN[key])
        records: list[dict[str, Any]] = []
        try:
            text = legacy.read_text(encoding="utf-8-sig")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn tail from a crash: not a label
            if isinstance(rec, dict):
                records.append(rec)
        _LEGACY_SEEN[key] = records
    print(
        f"PLATE: found {legacy} left by an older build ({len(records)} label(s)). It is read, never written: "
        f"labels now go to {target}. Move it there to keep them, or delete it.",
        file=sys.stderr,
        flush=True,
    )
    return list(records)


def _codes_file() -> Path:
    env = os.environ.get(CODES_ENV)
    return Path(env) if env else DEFAULT_CODES


@lru_cache(maxsize=4)
def _load_codes(path: str) -> dict[str, str]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    codes = raw.get("codes") if isinstance(raw, dict) else None
    if not isinstance(codes, dict) or not codes:
        raise VerdictError(f"{path}: expected a mapping with a non-empty 'codes' mapping")
    return {str(k): str(v) for k, v in codes.items()}


def reason_codes() -> dict[str, str]:
    """code -> description from calib/reason_codes.yaml (or `PLATE_REASON_CODES`)."""
    path = _codes_file()
    try:
        return dict(_load_codes(str(path)))
    except OSError as e:
        raise VerdictError(f"reason codes unreadable: {path} ({e})") from e
    except yaml.YAMLError as e:
        raise VerdictError(f"{path}: not valid YAML ({e})") from e


def check_reasons(verdict: str, reasons: Any) -> list[str]:
    """`reasons` de-duplicated in order. VerdictError for an unknown verdict or code, or a Ship with codes."""
    if verdict not in VERDICTS:
        raise VerdictError(f"unknown verdict {verdict!r}: expected one of {', '.join(VERDICTS)}")
    if isinstance(reasons, str) or not isinstance(reasons, list | tuple):
        raise VerdictError("reasons must be a list of reason codes")
    out = list(dict.fromkeys(reasons))
    if verdict not in NEEDS_REASONS:
        if out:
            raise VerdictError(f"verdict {verdict!r} takes no reason codes")
        return out
    if out:
        table = reason_codes()
        bad = [c for c in out if not isinstance(c, str) or c not in table]
        if bad:
            raise VerdictError(f"unknown reason code(s): {', '.join(str(b) for b in bad)}")
    return out


def _clean_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise VerdictError("seconds must be a number")
    secs = float(value)
    if not (0.0 <= secs <= MAX_SECONDS):  # false for NaN too
        raise VerdictError(f"seconds must be between 0 and {int(MAX_SECONDS)}")
    return round(secs, 1)


def build_label(
    *,
    at: datetime,
    rater: str,
    run_id: str,
    verdict: str,
    reasons: list[str],
    candidate: str,
    plate: str | None = None,
    gate_report: str | None = None,
    seconds: Any = None,
) -> dict[str, Any]:
    """One labels.jsonl record in the labelling tool's v1 schema (key order as `LabelRecord.to_dict`)."""
    rater = rater.strip() if isinstance(rater, str) else ""
    if not rater:
        raise VerdictError("rater must be a non-empty string")
    if not candidate:
        raise VerdictError("a label needs a candidate")
    rec: dict[str, Any] = {
        "v": LABEL_VERSION,
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rater": rater,
        "take_id": run_id,
        "plate": plate,
        "candidate": candidate,
        "run_id": run_id,
        "verdict": verdict,
        "reasons": list(reasons),
    }
    if seconds is not None:
        rec["seconds"] = _clean_seconds(seconds)
    if gate_report:
        rec["gate_report"] = gate_report
    return rec


def _ends_without_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except FileNotFoundError:
        return False


def append_label(path: Path | str, rec: dict[str, Any]) -> str:
    """Append `rec` as one line (UTF-8, LF) and flush it to disk. Returns the line that was written."""
    path = Path(path)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            lead = "\n" if _ends_without_newline(path) else ""
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(lead + line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    except OSError as e:
        raise VerdictError(f"cannot write the labels file {path}: {e}") from e
    return line
