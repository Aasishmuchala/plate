"""Label records and the append-only calib/labels.jsonl store: the record schema, a Windows-safe
one-line append, readers, the skip-already-labelled hash set and the image lister. New code for
PLATE (no seed script). Every file is UTF-8 with LF newlines on both OSes; image paths inside a
record are folder-relative with forward slashes so a labels file moves between Mac and Windows.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from blake3 import blake3

from plate_label import __version__
from plate_label.codes import validate_reasons

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})

# <repo>/calib/labels.jsonl (tools/plate_label/store.py -> parents[2] is the repo root). Gitignored.
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "calib" / "labels.jsonl"


def utc_now_iso() -> str:
    """ISO-8601 UTC with a trailing Z. Colons are fine inside file content, never in file names."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_blake3(path: Path) -> str:
    """Hex BLAKE3 of a file, streamed so a 16-bit TIFF never has to fit in memory twice."""
    h = blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_posix(path: Path, folder: Path) -> str:
    """Path of `path` relative to `folder`, forward slashes; ValueError when it is outside."""
    return Path(path).resolve().relative_to(Path(folder).resolve()).as_posix()


@dataclass
class LabelRecord:
    ts: str
    image: str
    image_blake3: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    note: str = ""
    rater: str = "unknown"
    take_id: str | None = None
    tool_version: str = __version__

    @classmethod
    def new(
        cls,
        image: str,
        image_blake3: str,
        verdict: str,
        reasons: list[str] | None = None,
        note: str = "",
        rater: str = "unknown",
        take_id: str | None = None,
    ) -> LabelRecord:
        return cls(
            ts=utc_now_iso(),
            image=image,
            image_blake3=image_blake3,
            verdict=verdict,
            reasons=list(reasons or []),
            note=note,
            rater=rater,
            take_id=take_id,
        )

    def to_json(self) -> str:
        """One line, no trailing newline; key order is the schema order."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


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


def append_label(path: Path, rec: LabelRecord) -> None:
    """Append one record as one line. A single write call so concurrent appenders cannot interleave."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if _ends_without_newline(path) else ""
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(lead + rec.to_json() + "\n")
        f.flush()


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


def labelled_hashes(path: Path) -> set[str]:
    """BLAKE3 hashes of every image that already has a label (used to skip them)."""
    return {r["image_blake3"] for r in read_labels(path) if r.get("image_blake3")}


def list_images(folder: Path) -> list[Path]:
    """Image files under `folder` (recursive, dot-files skipped), sorted case-insensitively by relative path."""
    folder = Path(folder)
    found: list[tuple[str, Path]] = []
    for p in folder.rglob("*"):
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            continue
        rel = p.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts):
            continue
        found.append((rel.as_posix(), p))
    found.sort(key=lambda t: (t[0].lower(), t[0]))
    return [p for _, p in found]


def record_label(
    out: Path,
    folder: Path,
    image: Path,
    verdict: str,
    reasons: list[str] | None = None,
    note: str = "",
    rater: str = "unknown",
    take_id: str | None = None,
) -> LabelRecord:
    """Validate, hash the file, append one record to `out` and return it.

    Raises ValueError for a bad verdict / reason set / O99 without a note, or an image outside `folder`.
    Every front end (terminal loop, --headless, the HTTP UI) goes through here.
    """
    codes = validate_reasons([] if reasons is None else reasons, verdict, note)
    rec = LabelRecord.new(
        image=rel_posix(image, folder),
        image_blake3=file_blake3(image),
        verdict=verdict,
        reasons=codes,
        note=note,
        rater=rater,
        take_id=take_id,
    )
    append_label(out, rec)
    return rec
