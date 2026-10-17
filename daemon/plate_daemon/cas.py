"""A project's content-addressed store: `<project_dir>/assets/<h[:2]>/<h>.<ext>`, keyed by BLAKE3 hex.

There is no global CAS (Decision 6): every project carries its own `assets/` so the folder alone is the
truth and the SQLite index can be rebuilt from it. Paths stay short and colon-free (about 80 characters
inside the project). Writes stream into `assets/.tmp/<uuid>` while hashing and move into place with
`os.replace`, retried with backoff (Windows and Defender hold new files briefly); an object is stored
once per hash whatever extension a later put names, so putting the same bytes twice is a no-op. Objects
are never symlinks and symlinked sources are refused. `on_put(hash, ext, bytes)` lets the daemon record
each put in the project index. `refcount` counts references to stored hashes in `project.json` and
`runs.jsonl`; it does not see references held only in the SQLite index (the seed worker's `payload_key`
and `result_key`) until those are ledgered.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

from blake3 import blake3

from .fsutil import replace_with_retry

KEY_RE = re.compile(r"^[0-9a-f]{64}$")
EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")
SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
OBJECT_RE = re.compile(r"^([0-9a-f]{64})\.([a-z0-9]{1,10})$")
CHUNK = 1 << 20
DEFAULT_EXT = "bin"
MAX_PATH_LEN = 259  # Windows MAX_PATH without the long-path opt-in, less the terminating NUL
TMP_DIR = ".tmp"

OnPut = Callable[[str, str, int], None]


class CASError(RuntimeError):
    pass


def hash_bytes(b: bytes) -> str:
    return blake3(b).hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    """(blake3 hex, size) of a file, streamed in 1 MiB chunks."""
    h = blake3()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def check_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise CASError(f"not a blake3 hex key: {key!r}")
    return key


def normalize_ext(ext: str | None) -> str:
    """`.PNG` -> `png`. Lower-case alphanumerics only: no dots, separators or ':' (Windows forbids it)."""
    if ext is None or ext == "":
        return DEFAULT_EXT
    if not isinstance(ext, str):
        raise CASError(f"extension must be a string, got {type(ext).__name__}")
    e = (ext[1:] if ext.startswith(".") else ext).lower()
    if not EXT_RE.match(e):
        raise CASError(f"refusing extension {ext!r}: 1-10 lower-case letters or digits, no ':' or separators")
    return e


def _iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


_HEX_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


class CAS:
    def __init__(self, root: Path, *, on_put: OnPut | None = None):
        """`root` is a project's `assets/` folder: `CAS(project_dir / "assets")`."""
        self.root = Path(root)
        self.tmp = self.root / TMP_DIR
        self.on_put = on_put

    # ------------------------------------------------------------------ locations

    def path_for(self, key: str, ext: str | None = DEFAULT_EXT) -> Path:
        """Where an object with this hash and extension lives (whether or not it exists)."""
        check_key(key)
        return self.root / key[:2] / f"{key}.{normalize_ext(ext)}"

    def find(self, key: str) -> Path | None:
        """The stored file for a hash, whatever its extension, or None."""
        shard = self.root / check_key(key)[:2]
        if not shard.is_dir():
            return None
        for p in sorted(shard.glob(f"{key}.*")):
            if OBJECT_RE.match(p.name) and p.is_file() and not p.is_symlink():
                return p
        return None

    def exists(self, key: str) -> bool:
        return self.find(key) is not None

    has = exists  # seed name, kept for the worker

    def iter_objects(self) -> Iterator[tuple[str, str, Path]]:
        """(hash, ext, path) for every stored object; temp files and stray names are skipped."""
        if not self.root.is_dir():
            return
        for shard in sorted(self.root.iterdir()):
            if not SHARD_RE.match(shard.name) or not shard.is_dir() or shard.is_symlink():
                continue
            for p in sorted(shard.iterdir()):
                m = OBJECT_RE.match(p.name)
                if m and m.group(1)[:2] == shard.name and p.is_file() and not p.is_symlink():
                    yield m.group(1), m.group(2), p

    # ------------------------------------------------------------------ writes

    def _new_tmp(self) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        return self.tmp / uuid.uuid4().hex

    def _commit(self, tmp: Path, key: str, ext: str, size: int) -> str:
        existing = self.find(key)
        if existing is None:
            dest = self.path_for(key, ext)
            if len(str(dest.absolute())) > MAX_PATH_LEN:
                raise CASError(
                    f"{dest}: longer than {MAX_PATH_LEN} characters (Windows MAX_PATH); move the project to a shorter path"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                replace_with_retry(tmp, dest)
            except PermissionError:
                # A concurrent put of the same bytes won the race and a reader holds it open: same content.
                if self.find(key) is None:
                    raise
            existing = self.find(key) or dest
        if self.on_put is not None:
            self.on_put(key, existing.name.rsplit(".", 1)[1], size)
        return key

    def put(
        self, data: bytes | bytearray | memoryview | str | os.PathLike[str], ext: str | None = None
    ) -> str:
        """Store bytes or a file's contents and return the BLAKE3 hex. A path's own suffix is the default
        extension. A `str` is a path, never content."""
        if isinstance(data, (bytes, bytearray, memoryview)):
            return self.put_bytes(bytes(data), ext)
        return self.put_file(Path(data), ext)

    def put_bytes(self, b: bytes, ext: str | None = None) -> str:
        e = normalize_ext(ext)
        key = hash_bytes(b)
        tmp = self._new_tmp()
        try:
            if self.find(key) is None:
                with open(tmp, "wb") as fh:
                    fh.write(b)
            return self._commit(tmp, key, e, len(b))
        finally:
            tmp.unlink(missing_ok=True)

    def put_file(self, path: Path, ext: str | None = None) -> str:
        """Copy a file in, hashing while copying so a file changing mid-copy cannot be mis-keyed."""
        path = Path(path)
        if path.is_symlink():
            raise CASError(f"refusing symlink {path}: the CAS copies real files only")
        e = normalize_ext(ext if ext is not None else (path.suffix or None))
        tmp = self._new_tmp()
        try:
            h = blake3()
            n = 0
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    h.update(chunk)
                    dst.write(chunk)
                    n += len(chunk)
            return self._commit(tmp, h.hexdigest(), e, n)
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ reads

    def open(self, key: str, mode: str = "rb") -> BinaryIO:
        """Read-only binary handle on an object (objects are immutable; `mode` must be "rb")."""
        if mode != "rb":
            raise CASError("CAS objects are immutable: open them with mode='rb'")
        p = self.find(key)
        if p is None:
            raise CASError(f"missing object {key}")
        return open(p, "rb")

    def read_bytes(self, key: str) -> bytes:
        with self.open(key) as fh:
            return fh.read()

    def verify(self, key: str) -> bool:
        p = self.find(key)
        if p is None:
            return False
        return hash_file(p)[0] == key

    # ------------------------------------------------------------------ references

    def refcount(self, project: Any = None) -> dict[str, int]:
        """{hash: references} for every stored object, counting each occurrence of the hash in
        `project.json` and `runs.jsonl` of `project` (a `Project`, a project folder, or None for this
        CAS's parent folder). Zero means nothing in the folder points at it (a `plate gc` candidate)."""
        project_dir = Path(getattr(project, "root", project) or self.root.parent)
        counts = {key: 0 for key, _ext, _p in self.iter_objects()}
        if not counts:
            return counts

        def tally(text: str) -> None:
            for m in _HEX_IN_TEXT.finditer(text):
                if m.group(0) in counts:
                    counts[m.group(0)] += 1

        pj = project_dir / "project.json"
        if pj.is_file():
            for s in _iter_strings(json.loads(pj.read_text(encoding="utf-8-sig"))):
                tally(s)
        ledger = project_dir / "runs.jsonl"
        if ledger.is_file():
            with open(ledger, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn last line is the ledger's business (T13), not the CAS's
                    for s in _iter_strings(rec):
                        tally(s)
        return counts
