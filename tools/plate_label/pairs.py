"""The things the labeller shows: a Pair is a plate (the render the candidate must stay faithful to)
and one candidate (an AI output, or a render under test), plus the take id and, optionally, the run id
and the path of the gate report that scored the candidate. Pairs come from a --pairs manifest (JSON list)
or from a folder scan. Nothing is copied: a Pair holds paths, the server streams them from where they
are. New code for PLATE. Paths in a record are forward-slash strings so a labels file moves between
Mac and Windows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})

_CANDIDATE_STEM = re.compile(r"^candidate(?:[._-].+|\d+)?$", re.IGNORECASE)
_FLAT_PLATE = re.compile(r"^(?P<take>.+?)[._-]plate$", re.IGNORECASE)
_FLAT_CANDIDATE = re.compile(r"^(?P<take>.+?)[._-]candidate(?:[._-].+|\d+)?$", re.IGNORECASE)


class PairsError(ValueError):
    """A manifest or folder that cannot be turned into pairs; the message says which entry and why."""


@dataclass(frozen=True)
class Pair:
    take_id: str
    candidate: Path  # absolute file the server streams
    candidate_ref: str  # what the label record stores (manifest string or folder-relative posix)
    plate: Path | None = None
    plate_ref: str | None = None
    run_id: str | None = None
    gate_report: str | None = None  # stored as written; calibration resolves it against its reports dir

    @property
    def key(self) -> tuple[str, str | None, str]:
        """Identity of a labelling target: the same take can have several candidates (best-of-N)."""
        return (self.take_id, self.plate_ref, self.candidate_ref)


def to_ref(text: str) -> str:
    """Manifest path string -> forward slashes (a Windows path typed with backslashes stays portable)."""
    return text.replace("\\", "/")


def _resolve_file(text: object, base: Path, what: str, where: str) -> tuple[Path, str]:
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise PairsError(f"{where}: {what} must be a non-empty path string")
    ref = to_ref(text.strip())
    path = Path(ref)
    if not path.is_absolute():
        path = base / path
    try:
        path = path.resolve()
    except (OSError, ValueError) as e:
        raise PairsError(f"{where}: {what} {text!r} is not a usable path ({e})") from e
    if path.suffix.lower() not in IMAGE_EXTS:
        raise PairsError(f"{where}: {what} {text!r} is not an image ({', '.join(sorted(IMAGE_EXTS))})")
    if not path.is_file():
        raise PairsError(f"{where}: {what} {text!r} does not exist")
    return path, ref


def load_manifest(manifest: Path) -> list[Pair]:
    """Read a --pairs JSON file: a list of {take_id, plate, candidate, gate_report, run_id?} (or
    {"pairs": [...]}). Relative paths are relative to the manifest's folder. Every problem is
    collected so one run reports them all."""
    manifest = Path(manifest)
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PairsError(f"pairs manifest not found: {manifest}") from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PairsError(f"{manifest}: not valid UTF-8 JSON ({e})") from e
    if isinstance(raw, dict) and isinstance(raw.get("pairs"), list):
        raw = raw["pairs"]
    if not isinstance(raw, list):
        raise PairsError(f"{manifest}: expected a JSON list of pairs")
    base = manifest.resolve().parent
    pairs: list[Pair] = []
    problems: list[str] = []
    seen: set[tuple[str, str | None, str]] = set()
    for i, entry in enumerate(raw):
        where = f"{manifest.name}[{i}]"
        try:
            pair = pair_from_entry(entry, base, where)
        except PairsError as e:
            problems.append(str(e))
            continue
        if pair.key in seen:
            problems.append(f"{where}: duplicate pair ({pair.take_id}, {pair.candidate_ref})")
            continue
        seen.add(pair.key)
        pairs.append(pair)
    if problems:
        raise PairsError("\n".join(problems))
    if not pairs:
        raise PairsError(f"{manifest}: no pairs listed")
    return pairs


def _optional_str(entry: dict, key: str, where: str) -> str | None:
    val = entry.get(key)
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        raise PairsError(f"{where}: {key} must be a non-empty string when given")
    return to_ref(val.strip()) if key == "gate_report" else val.strip()


def pair_from_entry(entry: object, base: Path, where: str) -> Pair:
    if not isinstance(entry, dict):
        raise PairsError(f"{where}: expected an object")
    take = entry.get("take_id")
    if not isinstance(take, str) or not take.strip() or len(take) > 200:
        raise PairsError(f"{where}: take_id must be a non-empty string (max 200 chars)")
    candidate, candidate_ref = _resolve_file(entry.get("candidate"), base, "candidate", where)
    plate = plate_ref = None
    if entry.get("plate") is not None:
        plate, plate_ref = _resolve_file(entry["plate"], base, "plate", where)
    return Pair(
        take_id=take.strip(),
        candidate=candidate,
        candidate_ref=candidate_ref,
        plate=plate,
        plate_ref=plate_ref,
        run_id=_optional_str(entry, "run_id", where),
        gate_report=_optional_str(entry, "gate_report", where),
    )


def _images_in(folder: Path) -> list[Path]:
    """Image files directly inside `folder`, dot-files skipped, sorted case-insensitively."""
    found = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    ]
    return sorted(found, key=lambda p: (p.name.lower(), p.name))


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def scan_folder(folder: Path) -> list[Pair]:
    """Pairs from a folder, by file-name convention (case-insensitive), in any directory under it:

    - take sub-folder: `<take>/plate.png` with `<take>/candidate.png`, `candidate_2.png`, `candidate-a.png` ...
    - flat: `<take>.plate.png` (or `_plate`, `-plate`) with `<take>.candidate.png`, `<take>_candidate_2.png` ...

    take_id is the sub-folder's path (first form) or the file prefix, relative to `folder` (second).
    If the folder holds no plate/candidate-named files at all, every image becomes a candidate-only
    pair (no plate, so no wipe): the plain "label a folder of images" use. Raises PairsError when there
    is nothing to label.
    """
    root = Path(folder).resolve()
    pairs: list[Pair] = []
    saw_roles = False
    plain: list[Pair] = []
    for directory in sorted(
        {root, *(p for p in root.rglob("*") if p.is_dir())}, key=lambda p: str(p).lower()
    ):
        rel_dir = directory.relative_to(root)
        if any(part.startswith(".") for part in rel_dir.parts):
            continue
        images = _images_in(directory)
        in_take_folder = directory != root
        plate_file = next((p for p in images if p.stem.lower() == "plate"), None) if in_take_folder else None
        cands = [p for p in images if _CANDIDATE_STEM.match(p.stem)] if in_take_folder else []
        if in_take_folder and (plate_file or cands):
            saw_roles = True
            for c in cands:
                pairs.append(
                    Pair(
                        take_id=rel_dir.as_posix(),
                        candidate=c,
                        candidate_ref=_rel(c, root),
                        plate=plate_file,
                        plate_ref=_rel(plate_file, root) if plate_file else None,
                    )
                )
            continue
        flat_plates: dict[str, Path] = {}
        flat_cands: list[tuple[str, Path]] = []
        for p in images:
            if m := _FLAT_PLATE.match(p.stem):
                flat_plates[m.group("take").lower()] = p
            elif m := _FLAT_CANDIDATE.match(p.stem):
                flat_cands.append((m.group("take"), p))
        for take, c in flat_cands:
            saw_roles = True
            plate_file = flat_plates.get(take.lower())
            pairs.append(
                Pair(
                    take_id=(rel_dir / take).as_posix() if rel_dir.parts else take,
                    candidate=c,
                    candidate_ref=_rel(c, root),
                    plate=plate_file,
                    plate_ref=_rel(plate_file, root) if plate_file else None,
                )
            )
        for p in images:
            plain.append(
                Pair(take_id=_rel(p, root).rsplit(".", 1)[0], candidate=p, candidate_ref=_rel(p, root))
            )
    if saw_roles:
        return pairs
    if not plain:
        raise PairsError(f"no images under {root}")
    return plain
